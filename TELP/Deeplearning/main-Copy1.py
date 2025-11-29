import argparse
import torch
import numpy as np
import time
import torch.nn.functional as F
import networkx as nx
import matplotlib.pyplot as plt
import random
import os
import pandas as pd

import torch_geometric.transforms as T
from torch_geometric.utils import negative_sampling, add_self_loops, degree, to_undirected, sort_edge_index, \
    from_networkx, to_networkx, to_dense_adj, to_edge_index, dense_to_sparse
from torch_geometric.datasets import AttributedGraphDataset, CitationFull, WebKB, Coauthor, Amazon, Actor
from torch_geometric.loader import DataLoader
from model import Encoder, EdgeDecoder, EncoderDecoder
from datetime import datetime
from sklearn.metrics import roc_auc_score, average_precision_score
import torch
from torch_geometric.data import Data

def evaluate_auc(val_pred, val_true, test_pred, test_true):
    valid_auc = roc_auc_score(val_true, val_pred)
    test_auc = roc_auc_score(test_true, test_pred)
    valid_ap = average_precision_score(val_true, val_pred)
    test_ap = average_precision_score(test_true, test_pred)
    results = dict()
    results['AUC'] = (valid_auc, test_auc)
    results['AP'] = (valid_ap, test_ap)
    return results

def train(model, data,train_data, optimizer, lr_scheduler, args, epoch):
    # 将模型设置为训练模式
    model.train()
    remaining_edges, masked_edges = train_data.edge_index, train_data.edge_index
    aug_edge_index, _ = add_self_loops(data.edge_index)
    neg_edges = negative_sampling(
        aug_edge_index,
        num_nodes=data.x.size(0),
        num_neg_samples=masked_edges.view(2, -1).size(1),
    ).view_as(masked_edges)
    for perm in DataLoader(range(masked_edges.size(1)), args.batch_size, shuffle=True):
        optimizer.zero_grad()
        z = model.encoder(data.x, remaining_edges)
        pos_scores = model.edge_decoder(z, masked_edges[:, perm])
        neg_scores = model.edge_decoder(z, neg_edges[:, perm])
        
        loss = F.binary_cross_entropy(pos_scores, torch.ones_like(pos_scores))
        loss += F.binary_cross_entropy(neg_scores, torch.zeros_like(neg_scores))


        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)

        optimizer.step()
        lr_scheduler.step()
        
    return loss.item()



@torch.no_grad()
def test(model, data, train_data, val_data, test_data, batch_size, args):
    # 将模型设置为测试模式
    model.eval()
    z = model.encoder(data.x, train_data.edge_index)
    pos_valid_edge = val_data.pos_edge_label_index
    neg_valid_edge = val_data.neg_edge_label_index

    pos_test_edge = test_data.pos_edge_label_index
    neg_test_edge = test_data.neg_edge_label_index

    pos_valid_preds = []
    for perm in DataLoader(range(pos_valid_edge.size(1)), batch_size):
        edge = pos_valid_edge[:, perm]
        pos_valid_preds += [model.edge_decoder(z, edge).squeeze().cpu()]
    pos_valid_pred = torch.cat(pos_valid_preds, dim=0)

    neg_valid_preds = []
    for perm in DataLoader(range(neg_valid_edge.size(1)), batch_size):
        edge = neg_valid_edge[:, perm]
        neg_valid_preds += [model.edge_decoder(z, edge).squeeze().cpu()]
    neg_valid_pred = torch.cat(neg_valid_preds, dim=0)

    pos_test_preds = []
    for perm in DataLoader(range(pos_test_edge.size(1)), batch_size):
        edge = pos_test_edge[:, perm]
        pos_test_preds += [model.edge_decoder(z, edge).squeeze().cpu()]
    pos_test_pred = torch.cat(pos_test_preds, dim=0)

    neg_test_preds = []
    for perm in DataLoader(range(neg_test_edge.size(1)), batch_size):
        edge = neg_test_edge[:, perm]
        neg_test_preds += [model.edge_decoder(z, edge).squeeze().cpu()]
    neg_test_pred = torch.cat(neg_test_preds, dim=0)

    val_pred = torch.cat([pos_valid_pred, neg_valid_pred], dim=0)
    val_true = torch.cat([torch.ones_like(pos_valid_pred), torch.zeros_like(neg_valid_pred)], dim=0)

    test_pred = torch.cat([pos_test_pred, neg_test_pred], dim=0)
    test_true = torch.cat([torch.ones_like(pos_test_pred), torch.zeros_like(neg_test_pred)], dim=0)

    results = evaluate_auc(val_pred, val_true, test_pred, test_true)
    return results

def load_network(dataset_path):
    edges_file = os.path.join(dataset_path, dataset_path.split('/')[-1]+"_edges.txt")

    G = nx.Graph()

    # 读取边文件
    with open(edges_file, "r") as f:
        for line in f:
            u, v = map(int, line.strip().split())
            G.add_edge(u, v)
            
    node_order = sorted(G.nodes())  # 按节点 ID 升序排序
    # 将节点 ID 映射到连续索引
    G = nx.relabel_nodes(G, {node: idx for idx, node in enumerate(node_order)})

    return G

def main():
    parser = argparse.ArgumentParser(description='')
    parser.add_argument('--device', type=int, default=0)
    parser.add_argument('--dataset', type=str, default='football')
    parser.add_argument('--encoder_channels', type=int, default=128)
    parser.add_argument('--hidden_channels', type=int, default=128)
    parser.add_argument('--encoder_dropout', type=float, default=0.)
    parser.add_argument('--decoder_dropout', type=float, default=0.)
    parser.add_argument('--lr', type=float, default=0.001)
    parser.add_argument('--epochs', type=int, default=500)
    parser.add_argument('--batch_size', type=int, default=2 ** 20)
    parser.add_argument('--t', type=int, default=2)
    parser.add_argument('--model', type=str, choices=['gcn', 'gat', 'graphsage'])
    path_dataset = './models'
    args = parser.parse_args()
    
    patience = int((args.epochs/args.t)/2)
    model_tag = f"model_{args.model}"
    results_dir = f"./results/{args.dataset}/{args.dataset}-{model_tag}-lr_{args.lr}-epochs_{args.epochs}-bz_{args.batch_size}-patience_{patience}-enc_{args.encoder_channels}-hic_{args.hidden_channels}-end_{args.encoder_dropout}-ded_{args.decoder_dropout}-t_{args.t}"
    os.makedirs(results_dir, exist_ok=True)

    device = f'cuda:{args.device}' if torch.cuda.is_available() else 'cpu'

    dataset_name = args.dataset
    edges_file_path = f"norm_dataset/{dataset_name}_edges.txt"
    G = nx.read_edgelist(edges_file_path, nodetype=int)

    # 将节点ID重映射为 0..N-1（保证连续）
    node_order = sorted(G.nodes())
    remap = {node: idx for idx, node in enumerate(node_order)}
    G = nx.relabel_nodes(G, remap)

    # 转成 PyG 的 edge_index 张量
    edge_index_np = np.array(list(G.edges()), dtype=np.int64).T  # shape [2, E]
    edge_index = torch.from_numpy(edge_index_np).long()

    # 人造节点属性：每个节点一维特征=1
    num_nodes = G.number_of_nodes()
    x = torch.ones((num_nodes, 1), dtype=torch.float)

    # 构造 PyG Data
    data = Data(x=x, edge_index=edge_index)

    # 数据转化：无向 + 放到设备
    transform = T.Compose([T.ToUndirected(), T.ToDevice(device)])
    data = transform(data)

    # 按照 data（而非 G）再取 num_nodes，保持一致
    num_nodes = data.num_nodes
    
    with torch.random.fork_rng():
        torch.manual_seed(42)
        np.random.seed(42)
        random.seed(42)
        
        transform = T.RandomLinkSplit(
            num_val=0.05,
            num_test=0.1,
            is_undirected=True,
            split_labels=True,
            add_negative_train_samples=True
        )
        train_data, val_data, test_data = transform(data)
    
    train_data.edge_index, _ = add_self_loops(train_data.edge_index, num_nodes=num_nodes)
    
    train_edge_index = train_data.edge_index.cpu().numpy().T  # 转换为边列表
    train_G = nx.Graph()
    train_G.add_nodes_from(range(data.num_nodes))  # 包含所有节点（但边仅限训练集）
    train_G.add_edges_from(train_edge_index)
    
 
    data.edge_index = data.edge_index.to(device)
    data.x = data.x.to(device)
    train_data.edge_index = train_data.edge_index.to(device)
    val_data.edge_index = val_data.edge_index.to(device)
    test_data.edge_index = test_data.edge_index.to(device)

    encoder = Encoder(data.x.size(1), args.encoder_channels, model_type=args.model)
    edge_decoder = EdgeDecoder(args.encoder_channels, args.hidden_channels, 1, args.decoder_dropout)
    model = EncoderDecoder(encoder, edge_decoder).to(device)
    print(model)
    print(device)
    tot_params = sum([np.prod(p.size()) for p in model.parameters()])
    print("Total number of parameters: {}".format(tot_params))
   
    model.reset_parameters()
    optimizer = torch.optim.Adam(params=model.parameters(), lr=args.lr)
    # 使用Cosine Annealing调度器，自动适配不同阶段的正则化需求
    lr_scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=int(args.epochs/args.t))
    best_valid_AUC = 0.0
    best_epoch = 0
    cnt_wait = 0
    epoch_records = []
    for epoch in range(1, 1 + args.epochs):
        t1 = time.time()
        loss = train(model, data, train_data, optimizer, lr_scheduler, args, epoch)
        t2 = time.time()
        results = test(model, data, train_data, val_data, test_data, args.batch_size, args)
        valid_result_AUC = results['AUC'][0]
        if valid_result_AUC > best_valid_AUC:
            best_valid_AUC = valid_result_AUC
            best_epoch = epoch
            cnt_wait = 0
        else:
            cnt_wait += 1                    
            
        for key, result in results.items():
            valid_result, test_result = result
            print(key)
            print(f'Epoch: {epoch:04d} / {args.epochs:02d}, '
                    f'Loss: {loss:.4f}, '
                    f'Valid: {valid_result:.2%}, '
                    f'Test: {test_result:.2%}',
                    f'Training Time/epoch: {t2 - t1:.3f}')
        valid_result_AP = results['AP'][0]
        valid_result_AUC = results['AUC'][0]
        test_result_AP = results['AP'][1]
        test_result_AUC = results['AUC'][1]
        epoch_data = {
                "epoch": epoch,
                "loss": loss,
                'valid_AP':valid_result_AP,
                'valid_AUC':valid_result_AUC,
                'test_AP':test_result_AP,
                'test_AUC':test_result_AUC,
                "training_time": (t2 - t1)
        }
        epoch_records.append(epoch_data)
            
        print('=' * round(140 * epoch / (args.epochs + 1)))
        if cnt_wait == patience:
            print('Early stopping!')
            break

    results = test(model, data, train_data, val_data, test_data, args.batch_size, args)

    for key, result in results.items():
        valid_result, test_result = result
        print(key)
        print(f'Best Epoch: {best_epoch:04d}, '
              f'Valid: {valid_result:.2%}, '
              f'Test: {test_result:.2%}')

    df_run = pd.DataFrame(epoch_records)
    current_time=time.time()
    run_excel_path = os.path.join(results_dir, f"{args.dataset}_{current_time}.xlsx")
    df_run.to_excel(run_excel_path, index=False)
    print(f"[INFO] {args.dataset}saved in: {run_excel_path}")

if __name__ == '__main__':
    main()
