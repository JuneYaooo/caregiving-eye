# -*- coding: utf-8 -*-
"""轻量 ST-GCN（训练用，需要 PyTorch；运行时只用导出的 ONNX）

结构：输入 BN → 6 个时空图卷积块（32→32→64↓→64→128↓→128）→ 全局池化 → 2 类
图卷积用固定的骨架邻接（自身 / 向心 / 离心三组，Yan et al. 2018 的空间划分）
加一份可学习的偏置邻接（ST-GCN++ 的做法），约 25 万参数。
"""
import os, sys
import numpy as np
import torch
import torch.nn as nn

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))
from mibutler.abilities.fall_gcn import EDGES, V   # noqa: E402


def adjacency():
    """返回 (3, V, V)：自身、向心（子→父）、离心（父→子），按入度归一化"""
    I = np.eye(V, dtype=np.float32)
    inward = np.zeros((V, V), np.float32)
    for parent, child in EDGES:
        inward[parent, child] = 1      # 第 parent 个输出聚合来自 child 的特征
    outward = inward.T.copy()

    def norm(a):
        d = a.sum(1, keepdims=True)
        return a / np.maximum(d, 1)
    return np.stack([I, norm(inward), norm(outward)])


class GraphConv(nn.Module):
    def __init__(self, cin, cout, A):
        super().__init__()
        self.register_buffer('A', torch.tensor(A))
        self.B = nn.Parameter(torch.zeros_like(self.A))
        self.K = A.shape[0]
        self.conv = nn.Conv2d(cin, cout * self.K, 1)
        self.cout = cout

    def forward(self, x):                      # x: (N, C, T, V)
        n, _, t, v = x.shape
        y = self.conv(x).view(n, self.K, self.cout, t, v)
        A = self.A + self.B                    # (K, V, V)：A[k, i, j] = 节点 j → 节点 i
        # 用 matmul 而不是 einsum，导出 ONNX 更稳
        y = torch.matmul(y, A.transpose(1, 2).unsqueeze(0).unsqueeze(2))
        return y.sum(1)


class Block(nn.Module):
    def __init__(self, cin, cout, A, stride=1, kt=5, drop=0.1):
        super().__init__()
        self.gcn = GraphConv(cin, cout, A)
        self.tcn = nn.Sequential(
            nn.BatchNorm2d(cout), nn.ReLU(inplace=True),
            nn.Conv2d(cout, cout, (kt, 1), (stride, 1), ((kt - 1) // 2, 0)),
            nn.BatchNorm2d(cout), nn.Dropout(drop))
        if cin == cout and stride == 1:
            self.res = nn.Identity()
        else:
            self.res = nn.Sequential(nn.Conv2d(cin, cout, 1, (stride, 1)), nn.BatchNorm2d(cout))
        self.relu = nn.ReLU(inplace=True)

    def forward(self, x):
        return self.relu(self.tcn(self.gcn(x)) + self.res(x))


class STGCN(nn.Module):
    def __init__(self, in_ch=3, n_class=2):
        super().__init__()
        A = adjacency()
        self.data_bn = nn.BatchNorm1d(in_ch * V)
        cfg = [(in_ch, 32, 1), (32, 32, 1), (32, 64, 2), (64, 64, 1), (64, 128, 2), (128, 128, 1)]
        self.blocks = nn.Sequential(*[Block(a, b, A, s) for a, b, s in cfg])
        self.fc = nn.Linear(128, n_class)

    def forward(self, x):                      # (N, 3, T, V)
        n, c, t, v = x.shape
        x = x.permute(0, 1, 3, 2).reshape(n, c * v, t)
        x = self.data_bn(x).view(n, c, v, t).permute(0, 1, 3, 2)
        x = self.blocks(x)
        return self.fc(x.mean(dim=(2, 3)))
