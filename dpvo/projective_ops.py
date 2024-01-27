import torch
import torch.nn.functional as F

from .lietorch import SE3, Sim3

MIN_DEPTH = 0.2

def extract_intrinsics(intrinsics):
    return intrinsics[...,None,None,:].unbind(dim=-1)

def coords_grid(ht, wd, **kwargs):
    y, x = torch.meshgrid(
        torch.arange(ht).to(**kwargs).float(),
        torch.arange(wd).to(**kwargs).float())

    return torch.stack([x, y], dim=-1)


def iproj(patches, intrinsics):
    """ inverse projection """
    x, y, d = patches.unbind(dim=2)
    fx, fy, cx, cy = intrinsics[...,None,None].unbind(dim=2)

    i = torch.ones_like(d)
    xn = (x - cx) / fx
    yn = (y - cy) / fy

    X = torch.stack([xn, yn, i, d], dim=-1)
    return X

def iproj_disps(disps, intrinsics):
    """ pinhole camera inverse projection """
    ht, wd = disps.shape[2:]
    fx, fy, cx, cy = extract_intrinsics(intrinsics)

    y, x = torch.meshgrid(
        torch.arange(ht).to(disps.device).float(),
        torch.arange(wd).to(disps.device).float())

    i = torch.ones_like(disps)
    X = (x - cx) / fx
    Y = (y - cy) / fy
    pts = torch.stack([X, Y, i, disps], dim=-1)

    return pts


def proj(X, intrinsics, depth=False):
    """ projection """

    X, Y, Z, W = X.unbind(dim=-1)
    fx, fy, cx, cy = intrinsics[...,None,None].unbind(dim=2)

    # d = 0.01 * torch.ones_like(Z)
    # d[Z > 0.01] = 1.0 / Z[Z > 0.01]
    # d = torch.ones_like(Z)
    # d[Z.abs() > 0.1] = 1.0 / Z[Z.abs() > 0.1]

    d = 1.0 / Z.clamp(min=0.1)
    x = fx * (d * X) + cx
    y = fy * (d * Y) + cy

    if depth:
        return torch.stack([x, y, d], dim=-1)

    return torch.stack([x, y], dim=-1)


def proj_disps(Xs, intrinsics, jacobian=False, return_depth=False):
    """ pinhole camera projection """
    fx, fy, cx, cy = extract_intrinsics(intrinsics)
    X, Y, Z, D = Xs.unbind(dim=-1)

    Z = torch.where(Z < 0.5*MIN_DEPTH, torch.ones_like(Z), Z)
    d = 1.0 / Z

    x = fx * (X * d) + cx
    y = fy * (Y * d) + cy
    if return_depth:
        coords = torch.stack([x, y, D*d], dim=-1)
    else:
        coords = torch.stack([x, y], dim=-1)

    if jacobian:
        B, N, H, W = d.shape
        o = torch.zeros_like(d)
        proj_jac = torch.stack([
             fx*d,     o, -fx*X*d*d,  o,
                o,  fy*d, -fy*Y*d*d,  o,
                # o,     o,    -D*d*d,  d,
        ], dim=-1).view(B, N, H, W, 2, 4)

        return coords, proj_jac

    return coords, None

def transform(poses, patches, intrinsics, ii, jj, kk, depth=False, valid=False, jacobian=False, tonly=False):
    """ projective transform """

    # backproject
    X0 = iproj(patches[:,kk], intrinsics[:,ii])

    # transform
    Gij = poses[:, jj] * poses[:, ii].inv()

    if tonly:
        Gij[...,3:] = torch.as_tensor([0,0,0,1], device=Gij.device)

    X1 = Gij[:,:,None,None] * X0

    # project
    x1 = proj(X1, intrinsics[:,jj], depth)


    if jacobian:
        p = X1.shape[2]
        X, Y, Z, H = X1[...,p//2,p//2,:].unbind(dim=-1)
        o = torch.zeros_like(H)
        i = torch.zeros_like(H)

        fx, fy, cx, cy = intrinsics[:,jj].unbind(dim=-1)

        d = torch.zeros_like(Z)
        d[Z.abs() > 0.2] = 1.0 / Z[Z.abs() > 0.2]

        Ja = torch.stack([
            H,  o,  o,  o,  Z, -Y,
            o,  H,  o, -Z,  o,  X,
            o,  o,  H,  Y, -X,  o,
            o,  o,  o,  o,  o,  o,
        ], dim=-1).view(1, len(ii), 4, 6)

        Jp = torch.stack([
             fx*d,     o, -fx*X*d*d,  o,
                o,  fy*d, -fy*Y*d*d,  o,
        ], dim=-1).view(1, len(ii), 2, 4)

        Jj = torch.matmul(Jp, Ja)
        Ji = -Gij[:,:,None].adjT(Jj)

        Jz = torch.matmul(Jp, Gij.matrix()[...,:,3:])

        return x1, (Z > 0.2).float(), (Ji, Jj, Jz)

    if valid:
        return x1, (X1[...,2] > 0.2).float()

    return x1

def point_cloud(poses, patches, intrinsics, ix):
    """ generate point cloud from patches """
    return poses[:,ix,None,None].inv() * iproj(patches, intrinsics[:,ix])


def flow_mag(poses, patches, intrinsics, ii, jj, kk, beta=0.3):
    """ projective transform """

    coords0 = transform(poses, patches, intrinsics, ii, ii, kk)
    coords1 = transform(poses, patches, intrinsics, ii, jj, kk, tonly=False)
    coords2 = transform(poses, patches, intrinsics, ii, jj, kk, tonly=True)

    flow1 = (coords1 - coords0).norm(dim=-1)
    flow2 = (coords2 - coords0).norm(dim=-1)

    return beta * flow1 + (1-beta) * flow2


def projective_transform(poses, depths, intrinsics, ii, jj):
    """ map points from ii->jj """

    # inverse project (pinhole)
    X0 = iproj_disps(depths[:,ii], intrinsics[:,ii])

    # transform
    Gij = poses[:,jj] * poses[:,ii].inv()

    Gij.data[:,ii==jj] = torch.as_tensor([-0.1, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0], device=torch.device('cuda'))
    X1 = Gij[:,:,None,None] * X0

    # project (pinhole)
    x1 = proj(X1, intrinsics[:,jj])

    # exclude points too close to camera
    valid = ((X1[...,2] > MIN_DEPTH) & (X0[...,2] > MIN_DEPTH)).float()
    valid = valid.unsqueeze(-1)

    return x1, valid


def induced_flow(poses, disps, intrinsics, ii, jj):
    """ optical flow induced by camera motion """

    ht, wd = disps.shape[2:]
    y, x = torch.meshgrid(
        torch.arange(ht).to(disps.device).float(),
        torch.arange(wd).to(disps.device).float())

    coords0 = torch.stack([x, y], dim=-1)
    coords1, valid = projective_transform(poses, disps, intrinsics, ii, jj)

    return coords1[...,:2] - coords0, valid
