# main.py
import argparse
import os
import time
import random
import numpy as np
import pandas as pd
import networkx as nx
import datetime

import torch
import torch.nn.functional as F
from torch_geometric.data import Data
import torch_geometric.transforms as T
from torch_geometric.loader import DataLoader
from torch_geometric.utils import negative_sampling, add_self_loops
from torch_geometric.utils import degree
from sklearn.metrics import roc_auc_score, average_precision_score
from sklearn.model_selection import KFold

from model import Encoder, EdgeDecoder, EncoderDecoder


# -------------------- 评价、训练、测试（与你一致） --------------------
def evaluate_auc(val_pred, val_true, test_pred, test_true):
    valid_auc = roc_auc_score(val_true, val_pred)
    test_auc = roc_auc_score(test_true, test_pred)
    valid_ap = average_precision_score(val_true, val_pred)
    test_ap = average_precision_score(test_true, test_pred)
    results = dict()
    results['AUC'] = (valid_auc, test_auc)
    results['AP'] = (valid_ap, test_ap)
    return results


def train(model, data, train_data, optimizer, lr_scheduler, args, epoch):
    model.train()
    remaining_edges, masked_edges = train_data.edge_index, train_data.edge_index

    # 训练阶段：基于“全图 + 自环”的边进行在线负采样
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
    model.eval()
    z = model.encoder(data.x, train_data.edge_index)

    pos_valid_edge = val_data.pos_edge_label_index
    neg_valid_edge = val_data.neg_edge_label_index
    pos_test_edge = test_data.pos_edge_label_index
    neg_test_edge = test_data.neg_edge_label_index

    def batched_scores(edge_idx):
        preds = []
        for perm in DataLoader(range(edge_idx.size(1)), batch_size):
            edge = edge_idx[:, perm]
            preds += [model.edge_decoder(z, edge).squeeze().cpu()]
        return torch.cat(preds, dim=0) if preds else torch.empty(0)

    pos_valid_pred = batched_scores(pos_valid_edge)
    neg_valid_pred = batched_scores(neg_valid_edge)
    pos_test_pred = batched_scores(pos_test_edge)
    neg_test_pred = batched_scores(neg_test_edge)

    val_pred = torch.cat([pos_valid_pred, neg_valid_pred], dim=0)
    val_true = torch.cat([torch.ones_like(pos_valid_pred), torch.zeros_like(neg_valid_pred)], dim=0)

    test_pred = torch.cat([pos_test_pred, neg_test_pred], dim=0)
    test_true = torch.cat([torch.ones_like(pos_test_pred), torch.zeros_like(neg_test_pred)], dim=0)

    results = evaluate_auc(val_pred, val_true, test_pred, test_true)
    return results
# -------------------------------------------------------------------


# -------------------- 小工具 --------------------
def make_undirected_unique_edges(G):
    """将无向边标准化为 (u<v) 并去重"""
    edges = []
    for u, v in G.edges():
        a, b = (u, v) if u < v else (v, u)
        edges.append((a, b))
    return sorted(list(set(edges)))


def to_index_tensor(edge_list, device):
    """[(u,v)] -> tensor shape [2, E]"""
    if not edge_list:
        return torch.empty((2, 0), dtype=torch.long, device=device)
    arr = np.array(edge_list, dtype=np.int64).T
    return torch.from_numpy(arr).long().to(device)


def bidir_edge_index(edge_list, device):
    """[(u,v)] -> 双向 edge_index tensor shape [2, 2E]"""
    if not edge_list:
        return torch.empty((2, 0), dtype=torch.long, device=device)
    bi = []
    for u, v in edge_list:
        bi.append((u, v))
        bi.append((v, u))
    arr = np.array(bi, dtype=np.int64).T
    return torch.from_numpy(arr).long().to(device)
# -------------------------------------------------------------------


# -------------------- 五折交叉主流程 --------------------
def link_prediction_cross_validation(G, data_full, args, device):
    """
    给定 NetworkX 全图 G 与 PyG 全图 data_full（x=1，edge_index=全边双向），
    在正样本边上做 5 折交叉：每折 4 份边组成训练图（再切 10% 做验证），1 份边作测试正样本；
    验证/测试负样本来自非边随机采样（同量）。
    """
    all_positive_edges = make_undirected_unique_edges(G)  # u<v
    kf = KFold(n_splits=5, shuffle=True, random_state=42)

    patience = int((args.epochs / args.t) / 2)
    rng_global = np.random.default_rng(42)
    nodes = list(G.nodes())
    existing_edges = set(all_positive_edges)  # 全图正边集合（u<v）

    fold_rows = []
    model_key = args.model
    test_auc_list, test_ap_list = [], []

    for fold, (train_idx, test_idx) in enumerate(kf.split(all_positive_edges), start=1):
        print(f"\n----- Fold {fold}/5 -----")

        # === 1) 正样本划分 ===
        train_pos_edges = [all_positive_edges[i] for i in train_idx]
        test_pos_edges = [all_positive_edges[i] for i in test_idx]

        # === 2) 训练子图（仅含 train_pos，避免信息泄漏）===
        G_train = nx.Graph()
        G_train.add_nodes_from(G.nodes())
        G_train.add_edges_from(train_pos_edges)

        # === 3) 负样本（来自非边；与对应正样本数量一致）===
        # 测试负样本
        test_neg_edges, test_neg_set = [], set()
        while len(test_neg_edges) < len(test_pos_edges):
            u = random.choice(nodes)
            v = random.choice(nodes)
            if u == v:
                continue
            pair = (u, v) if u < v else (v, u)
            if pair in existing_edges or pair in test_neg_set:
                continue
            test_neg_set.add(pair)
            test_neg_edges.append((u, v))

        # 训练负样本
        train_neg_edges, train_neg_set = [], set()
        while len(train_neg_edges) < len(train_pos_edges):
            u = random.choice(nodes)
            v = random.choice(nodes)
            if u == v:
                continue
            pair = (u, v) if u < v else (v, u)
            if pair in existing_edges or pair in train_neg_set or pair in test_neg_set:
                continue
            train_neg_set.add(pair)
            train_neg_edges.append((u, v))

        # === 4) 从训练正样本切 10% 做验证（负样本同步切）===
        rng = np.random.default_rng(42 + fold)
        rng.shuffle(train_pos_edges)
        rng.shuffle(train_neg_edges)
        n_val = max(1, int(0.10 * len(train_pos_edges)))

        val_pos_edges = train_pos_edges[:n_val]
        final_train_pos = train_pos_edges[n_val:]

        val_neg_edges = train_neg_edges[:n_val]
        # final_train_neg = train_neg_edges[n_val:]  # 训练阶段你是在线负采样，这里不再使用

        # === 5) 组装 PyG 数据对象 ===
        # 训练图：仅含最终训练正边（双向）
        train_data = Data(edge_index=bidir_edge_index(final_train_pos, device))

        # 验证/测试集：各自的正/负边索引（注意是 u,v 不需要双向）
        val_data = Data()
        val_data.pos_edge_label_index = to_index_tensor(val_pos_edges, device)
        val_data.neg_edge_label_index = to_index_tensor(val_neg_edges, device)

        test_data = Data()
        test_data.pos_edge_label_index = to_index_tensor(test_pos_edges, device)
        test_data.neg_edge_label_index = to_index_tensor(test_neg_edges, device)

        # === 6) 模型、训练、评估 ===
        encoder = Encoder(data_full.x.size(1), args.encoder_channels, model_type=model_key)
        edge_decoder = EdgeDecoder(args.encoder_channels, args.hidden_channels, 1, args.decoder_dropout)
        model = EncoderDecoder(encoder, edge_decoder).to(device)

        model.reset_parameters()
        optimizer = torch.optim.Adam(params=model.parameters(), lr=args.lr)
        lr_scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=int(args.epochs / args.t))

        # 训练图加自环（与 GCNConv(add_self_loops=False) 对应）
        train_data.edge_index, _ = add_self_loops(train_data.edge_index, num_nodes=data_full.num_nodes)

        best_valid_AUC, best_epoch, cnt_wait = 0.0, 0, 0
        best_state = None

        for epoch in range(1, args.epochs + 1):
            t1 = time.time()
            loss = train(model, data_full, train_data, optimizer, lr_scheduler, args, epoch)
            t2 = time.time()
            results = test(model, data_full, train_data, val_data, test_data, args.batch_size, args)

            valid_AUC = results['AUC'][0]
            valid_AP = results['AP'][0]
            test_AUC = results['AUC'][1]
            test_AP = results['AP'][1]

            print(f'Epoch {epoch:04d}/{args.epochs:04d} | Loss {loss:.4f} | '
                  f'Valid AUC/AP {valid_AUC:.2%}/{valid_AP:.2%} | '
                  f'Test AUC/AP {test_AUC:.2%}/{test_AP:.2%} | '
                  f'{t2 - t1:.3f}s')

            if valid_AUC > best_valid_AUC:
                best_valid_AUC = valid_AUC
                best_epoch = epoch
                best_state = {k: v.detach().cpu().clone() for k, v in model.state_dict().items()}
                cnt_wait = 0
            else:
                cnt_wait += 1

            if cnt_wait == patience:
                print('Early stopping!')
                break

        if best_state is not None:
            model.load_state_dict(best_state)

        final = test(model, data_full, train_data, val_data, test_data, args.batch_size, args)
        fold_rows.append({
            'fold': fold,
            'valid_auc': final['AUC'][0],
            'test_auc': final['AUC'][1],
            'valid_ap': final['AP'][0],
            'test_ap': final['AP'][1],
            'best_epoch': best_epoch
        })

        print(f'**** Fold {fold} | Best Epoch {best_epoch:04d} | '
              f'Valid AUC/AP {final["AUC"][0]:.2%}/{final["AP"][0]:.2%} | '
              f'Test AUC/AP {final["AUC"][1]:.2%}/{final["AP"][1]:.2%}')
        test_auc_list.append(final['AUC'][1])
        test_ap_list.append(final['AP'][1])

    # === 汇总 5 折 ===
    # df = pd.DataFrame(fold_rows)
    # avg_row = {
    #     'valid_auc': df['valid_auc'].mean(),
    #     'test_auc': df['test_auc'].mean(),
    #     'valid_ap': df['valid_ap'].mean(),
    #     'test_ap': df['test_ap'].mean(),
    # }

    # return avg_row
    return {
        'test_auc_mean': np.mean(test_auc_list),
        'test_ap_mean': np.mean(test_ap_list)
    }

# -------------------------------------------------------------------


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
    parser.add_argument('--runs', type=int, default=2)
    parser.add_argument('--model', type=str, choices=['gcn', 'gat', 'graphsage'])
    args = parser.parse_args()

    patience = int((args.epochs / args.t) * 0.1)
    model_tag = f"model_{args.model}-cv5"
    results_dir = f"./results/{args.dataset}/{args.dataset}-{model_tag}-lr_{args.lr}-epochs_{args.epochs}-bz_{args.batch_size}-patience_{patience}-enc_{args.encoder_channels}-hic_{args.hidden_channels}-end_{args.encoder_dropout}-ded_{args.decoder_dropout}-t_{args.t}"
    os.makedirs(results_dir, exist_ok=True)

    device = f'cuda:{args.device}' if torch.cuda.is_available() else 'cpu'
    print("Using device:", device)
    if torch.cuda.is_available():
        print("GPU:", torch.cuda.get_device_name(args.device))

    # ====== 读取仅含连边的图；节点属性置为 1 ======
    dataset_name = args.dataset
    edges_file_path = f"norm_dataset/{dataset_name}_edges.txt"
    G = nx.read_edgelist(edges_file_path, nodetype=int)

    # 节点 ID 重映射到 0..N-1
    node_order = sorted(G.nodes())
    remap = {node: idx for idx, node in enumerate(node_order)}
    G = nx.relabel_nodes(G, remap)

    # 构造 PyG Data（全图：双向 edge_index + x=全1）
    undirected_edges = make_undirected_unique_edges(G)
    bidir_edges = []
    for u, v in undirected_edges:
        bidir_edges.append((u, v))
        bidir_edges.append((v, u))
    edge_index_full = torch.tensor(bidir_edges, dtype=torch.long).t().contiguous()
    deg = degree(edge_index_full[0], num_nodes=G.number_of_nodes()) 
    x = torch.log1p(deg).view(-1, 1).to(torch.float32) 
    data_full = Data(x=x, edge_index=edge_index_full)

    #x = torch.ones((G.number_of_nodes(), 1), dtype=torch.float)
    #data_full = Data(x=x, edge_index=edge_index_full)
    data_full = T.ToDevice(device)(data_full)

    # runs 循环
    all_runs = []
    for run in range(args.runs):
        print(f"\n===== Run {run + 1}/{args.runs} =====")
        results = link_prediction_cross_validation(G, data_full, args, device)
        row = {
            'run': run + 1,
            'test_auc_mean': results['test_auc_mean'],
            'test_ap_mean': results['test_ap_mean']
        }
        all_runs.append(row)

    df = pd.DataFrame(all_runs)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    file_path = os.path.join(results_dir, f"{args.dataset}_results_{timestamp}.xlsx")
    df.to_excel(file_path, index=False)
    print(f"\n[INFO] Results saved to {file_path}")

if __name__ == '__main__':
    main()