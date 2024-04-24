#!/bin/sh

conda activate rlgpu
export OLD_LD_LIBRARY_PATH=$LD_LIBRARY_PATH
export LD_LIBRARY_PATH=/home/kunal/miniconda3/envs/rlgpu/lib:$LD_LIBRARY_PATH
export OLD_LD_LIBRARY_PATH=$LD_LIBRARY_PATH
export LD_LIBRARY_PATH=/home/kudos/miniconda3/envs/rlgpu/lib/python3.7/site-packages/nvidia/cublas/lib/:$OLD_LD_LIBRARY_PATH
