"""
    Action Scoring Module only
"""


import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import DataLoader

# https://github.com/erikwijmans/Pointnet2_PyTorch
from pointnet2_ops.pointnet2_modules import PointnetFPModule, PointnetSAModule
from pointnet2.models.pointnet2_ssg_cls import PointNet2ClassificationSSG


class PointNet2SemSegSSG(PointNet2ClassificationSSG):
    def _build_model(self):
        self.SA_modules = nn.ModuleList()
        self.SA_modules.append(
            PointnetSAModule(
                npoint=1024,
                radius=0.1,
                nsample=32,
                mlp=[3, 32, 32, 64],
                use_xyz=True,
            )
        )
        self.SA_modules.append(
            PointnetSAModule(
                npoint=256,
                radius=0.2,
                nsample=32,
                mlp=[64, 64, 64, 128],
                use_xyz=True,
            )
        )
        self.SA_modules.append(
            PointnetSAModule(
                npoint=64,
                radius=0.4,
                nsample=32,
                mlp=[128, 128, 128, 256],
                use_xyz=True,
            )
        )
        self.SA_modules.append(
            PointnetSAModule(
                npoint=16,
                radius=0.8,
                nsample=32,
                mlp=[256, 256, 256, 512],
                use_xyz=True,
            )
        )

        self.FP_modules = nn.ModuleList()
        self.FP_modules.append(PointnetFPModule(mlp=[128 + 3, 128, 128, 128]))
        self.FP_modules.append(PointnetFPModule(mlp=[256 + 64, 256, 128]))
        self.FP_modules.append(PointnetFPModule(mlp=[256 + 128, 256, 256]))
        self.FP_modules.append(PointnetFPModule(mlp=[512 + 256, 256, 256]))

        self.fc_layer = nn.Sequential(
            nn.Conv1d(128, self.hparams['feat_dim'], kernel_size=1, bias=False),
            nn.BatchNorm1d(self.hparams['feat_dim']),
            nn.ReLU(True),
        )

    def forward(self, pointcloud): # [32, 10000, 6]
        r"""
            Forward pass of the network

            Parameters
            ----------
            pointcloud: Variable(torch.cuda.FloatTensor)
                (B, N, 3 + input_channels) tensor
                Point cloud to run predicts on
                Each point in the point-cloud MUST
                be formated as (x, y, z, features...)
        """
        xyz, features = self._break_up_pc(pointcloud) # [32, 10000, 3]  [32, 3, 10000]

        l_xyz, l_features = [xyz], [features]
        for i in range(len(self.SA_modules)): # 4
          # [32, 1024, 3]  [32, 64, 1024]
            li_xyz, li_features = self.SA_modules[i](l_xyz[i], l_features[i]) # [32, 10000, 3]  [32, 3, 10000]
            l_xyz.append(li_xyz)
            l_features.append(li_features)

        for i in range(-1, -(len(self.FP_modules) + 1), -1):
            l_features[i - 1] = self.FP_modules[i](
                l_xyz[i - 1], l_xyz[i], l_features[i - 1], l_features[i]
            )

        return self.fc_layer(l_features[0])


class Critic(nn.Module):
    def __init__(self, feat_dim):
        super(Critic, self).__init__()

        self.mlp1 = nn.Linear(feat_dim+3+3+1, feat_dim)
        self.mlp2 = nn.Linear(feat_dim, 1)

        self.BCELoss = nn.BCEWithLogitsLoss(reduction='none')

    # pixel_feats B x F, query_fats: B x 6
    # output: B
    def forward(self, pixel_feats, query_feats): # [32, 128] [32, 6]
        net = torch.cat([pixel_feats, query_feats], dim=-1) # [32, 134]
        net = F.leaky_relu(self.mlp1(net)) # [32, 128]
        net = self.mlp2(net).squeeze(-1) # [32]
        return net
     
    # cross entropy loss
    def get_ce_loss(self, pred_logits, gt_labels):
        loss = self.BCELoss(pred_logits, gt_labels.float())
        return loss


class Network(nn.Module):
    def __init__(self, feat_dim):
        super(Network, self).__init__()
        
        self.pointnet2 = PointNet2SemSegSSG({'feat_dim': feat_dim})
        
        self.critic = Critic(feat_dim)

    # pcs: B x N x 3 (float), with the 0th point to be the query point
    # pred_result_logits: B, whole_feats: B x F x N
    def forward(self, pcs, dirs1, dirs2, depth):
        pcs = pcs.repeat(1, 1, 2) # [32, 10000, 6]
        whole_feats = self.pointnet2(pcs) # [32, 128, 10000] 输出了 128 维的特征向量，并且这些特征向量对应于原始点云中的每个点

        net = whole_feats[:, :, 0] # [32, 128] 只取第 0 个像素的特征

        input_queries = torch.cat([dirs1, dirs2, depth], dim=1) # [32, 6]

        pred_result_logits = self.critic(net, input_queries) # [32]

        return pred_result_logits, whole_feats

    def inference_whole_pc(self, feats, dirs1, dirs2, depth): # [32, 128, 10000]  [32, 3]  [32, 3]
        num_pts = feats.shape[-1] # 10000
        batch_size = feats.shape[0] # 32

        feats = feats.permute(0, 2, 1)  # B x N x F  [32, 10000, 128]
        feats = feats.reshape(batch_size*num_pts, -1) # [320000, 128]

        input_queries = torch.cat([dirs1, dirs2, depth], dim=-1)
        input_queries = input_queries.unsqueeze(dim=1).repeat(1, num_pts, 1) # [32, 10000, 6]
        input_queries = input_queries.reshape(batch_size*num_pts, -1)

        pred_result_logits = self.critic(feats, input_queries) # [320000, 128]  [320000, 6] -> [320000]
        
        soft_pred_results = torch.sigmoid(pred_result_logits)
        soft_pred_results = soft_pred_results.reshape(batch_size, num_pts)

        return soft_pred_results

    def inference(self, pcs, dirs1, dirs2, depth):
        pcs = pcs.repeat(1, 1, 2)
        whole_feats = self.pointnet2(pcs)

        net = whole_feats[:, :, 0]

        input_queries = torch.cat([dirs1, dirs2, depth], dim=1)

        pred_result_logits = self.critic(net, input_queries)

        pred_results = (pred_result_logits > 0)

        return pred_results
 
