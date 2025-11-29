import math
import torch
import torch_geometric
import networkx as nx
import torch.nn.functional as F
from torch_geometric.utils import degree, add_self_loops, from_networkx, to_networkx
from torch_geometric.nn import GATConv, GINConv, GCNConv, MessagePassing, GraphSAGE
from torch_geometric.nn import SAGEConv
from torch_geometric.data import Data

# 编码器 Encoder：利用 GCNCN 层生成节点嵌入。
class Encoder(torch.nn.Module):
    def __init__(self, in_channels, out_channels, model_type, dropout_p=0.5):
        super(Encoder, self).__init__()
        self.model_type = model_type

        if self.model_type == 'gcn':
            self.conv = GCNConv(
                in_channels,
                out_channels,
                add_self_loops=False,
                normalize=True
            )
        elif self.model_type == 'gat':
            # 使用 concat=False 得到维度与 out_channels 一致（非 heads*out_channels）
            self.conv = GATConv(
                in_channels,
                out_channels,
                heads=4,
                concat=False,
                add_self_loops=False
            )
        elif self.model_type == 'graphsage':
            self.conv = SAGEConv(
                in_channels,
                out_channels,
                normalize=False
            )
        else:
            raise ValueError(f"Unknown model_type: {self.model_type}")

    def reset_parameters(self):
        self.conv.reset_parameters()
        
    # forward 方法通过 GCNCN 层获得节点嵌入。
    def forward(self, x, edge_index):
        # 调用 GCNCN 的前向传播
        x = self.conv(x, edge_index)
        return x

# 解码器 EdgeDecoder：基于嵌入生成边缘预测（是否存在连接）。
class EdgeDecoder(torch.nn.Module):
    def __init__(self, in_channels, hidden_channels, out_channels=1, dropout=0.5):
        super(EdgeDecoder, self).__init__()
        self.fc1 = torch.nn.Linear(in_channels, hidden_channels)
        self.fc2 = torch.nn.Linear(hidden_channels, out_channels)
        self.dropout = torch.nn.Dropout(dropout)

    def reset_parameters(self):
        self.fc1.reset_parameters()
        self.fc2.reset_parameters()

    def forward(self, z, edge):
         # 将两个节点嵌入相乘，用两个节点嵌入的 Hadamard 积表示连接特征。
        x = z[edge[0]] * z[edge[1]]
        x = self.fc1(x)
        x = self.dropout(x)
        # 使用 Mish 激活函数
        x = F.mish(x)
        x = self.fc2(x)
        x = self.dropout(x)
        # 用 Sigmoid 得到连接概率
        probs = torch.sigmoid(x)
        return probs

# EncoderDecoder 类将编码器和解码器整合在一起，构成一个完整的自编码器。
class EncoderDecoder(torch.nn.Module):
    def __init__(self, encoder, edge_decoder):
        super(EncoderDecoder, self).__init__()
        self.encoder = encoder
        self.edge_decoder = edge_decoder

    def reset_parameters(self):
        self.encoder.reset_parameters()
        self.edge_decoder.reset_parameters()
        
    # forward 方法仅运行编码器部分，将节点特征转换为嵌入表示。
    def forward(self, x, edge_index):
        return self.encoder(x, edge_index)
