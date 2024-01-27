#!/bin/bash -l
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=4
#SBATCH --gres=gpu:4
#SBATCH --time=00:30:00

source activate $1

export NCCL_DEBUG=INFO
export PYTHONFAULTHANDLER=1

srun python train.py
