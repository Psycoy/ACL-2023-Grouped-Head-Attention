# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import math
from dataclasses import dataclass, field
from requests import head

import torch
from torch import linalg as LA
from fairseq import metrics, utils
from fairseq.criterions import FairseqCriterion, register_criterion
from fairseq.dataclass import FairseqDataclass
from omegaconf import II
from fairseq.dataclass import ChoiceEnum


@dataclass
class LabelSmoothedCrossEntropyCriterionConfig_headclustering(FairseqDataclass):
    label_smoothing: float = field(
        default=0.0,
        metadata={"help": "epsilon for label smoothing, 0 means no label smoothing"},
    )
    report_accuracy: bool = field(
        default=False,
        metadata={"help": "report accuracy metric"},
    )
    ignore_prefix_size: int = field(
        default=0,
        metadata={"help": "Ignore first N tokens"},
    )
    sentence_avg: bool = II("optimization.sentence_avg")
    
    ########################################################################################
    
    Supervise_mode: ChoiceEnum(['reconstruction', 'classification']) = field(
        default="reconstruction",
        metadata={"help": "Which clustering supervision mode to use."},
    )
    
    cluster_matrix: ChoiceEnum(['Q', 'K', 'V', 'attn_matrix', 'headout']) = field(
        default="headout",
        metadata={"help": "Sample to cluster on."},
    )
    
    supervised_matrix: ChoiceEnum(['Q', 'K', 'V', 'attn_matrix', 'headout']) = field(
        default="headout",
        metadata={"help": "Sample to compute the distance with cluster center."},
    )
    
    cluster_loss_coefficient_inclass: float = field(
        default=1.0,
        metadata={"help": "Coefficient for the clustering loss."},
    )
    cluster_loss_coefficient_interclass: float = field(
        default=1.0,
        metadata={"help": "Coefficient for the clustering loss."},
    )
    
    N_head_clusters: int = field(
        default=2,
        metadata={"help": "How many clusters to use."},
    )

    use_interclass_loss: bool = field(
        default=False,
        metadata={"help": "Whether to use loss between classes."},
    )
    
    use_inclass_loss: bool = field(
        default=False,
        metadata={"help": "whether to use loss within classes."},
    )

def label_smoothed_nll_loss(lprobs, target, epsilon, ignore_index=None, reduce=True):
    if target.dim() == lprobs.dim() - 1:
        target = target.unsqueeze(-1)
    nll_loss = -lprobs.gather(dim=-1, index=target)
    smooth_loss = -lprobs.sum(dim=-1, keepdim=True)
    if ignore_index is not None:
        pad_mask = target.eq(ignore_index)
        nll_loss.masked_fill_(pad_mask, 0.0)
        smooth_loss.masked_fill_(pad_mask, 0.0)
    else:
        nll_loss = nll_loss.squeeze(-1)
        smooth_loss = smooth_loss.squeeze(-1)
    if reduce:
        nll_loss = nll_loss.sum()
        smooth_loss = smooth_loss.sum()
    eps_i = epsilon / (lprobs.size(-1) - 1)
    loss = (1.0 - epsilon - eps_i) * nll_loss + eps_i * smooth_loss
    return loss, nll_loss


@register_criterion(
    "label_smoothed_cross_entropy_headclustering_prune", dataclass=LabelSmoothedCrossEntropyCriterionConfig_headclustering
)
class LabelSmoothedCrossEntropyCriterion_headclustering(FairseqCriterion):
    def __init__(
        self,
        task,
        sentence_avg,
        label_smoothing,
        ignore_prefix_size=0,
        report_accuracy=False,
        cfg=LabelSmoothedCrossEntropyCriterionConfig_headclustering
    ):
        super().__init__(task)
        self.cfg =cfg
        self.sentence_avg = sentence_avg
        self.eps = label_smoothing
        self.ignore_prefix_size = ignore_prefix_size
        self.report_accuracy = report_accuracy
        
        self.cluster_matrix = cfg.cluster_matrix
        self.supervised_matrix = cfg.supervised_matrix
        self.cluster_loss_coefficient_inclass = cfg.cluster_loss_coefficient_inclass
        self.cluster_loss_coefficient_interclass = cfg.cluster_loss_coefficient_interclass
        self.N_head_clusters = cfg.N_head_clusters
        
        
    def add_cluster_loss(self, net_output, Regu): # TODO uncompleted
        Regu_init = Regu
        if self.cfg.Supervise_mode == "reconstruction":
            if self.cluster_matrix == "Q":
                cluster_centers_layers = net_output[1]["decoder_head_matrices"]["cluster_centers_Q"]
                head_labels_layers = net_output[1]["decoder_head_matrices"]["head_labels_Q"]
            elif self.cluster_matrix == "K":
                cluster_centers_layers = net_output[1]["decoder_head_matrices"]["cluster_centers_K"]
                head_labels_layers = net_output[1]["decoder_head_matrices"]["head_labels_K"]
            elif self.cluster_matrix == "V":
                cluster_centers_layers = net_output[1]["decoder_head_matrices"]["cluster_centers_V"]
                head_labels_layers = net_output[1]["decoder_head_matrices"]["head_labels_V"]
            elif self.cluster_matrix == "attn_matrix":
                cluster_centers_layers = net_output[1]["decoder_head_matrices"]["cluster_centers_attention_matrices"]
                head_labels_layers = net_output[1]["decoder_head_matrices"]["head_labels_attention_matrices"]
            elif self.cluster_matrix == "headout":
                cluster_centers_layers = net_output[1]["decoder_head_matrices"]["cluster_centers_headout"]
                head_labels_layers = net_output[1]["decoder_head_matrices"]["head_labels_headout"]
                
            if self.supervised_matrix == "Q":
                supervised_matrices_layers = net_output[1]["decoder_head_matrices"]["q_matrices_tobesupervised"]
            elif self.supervised_matrix == "K":
                supervised_matrices_layers = net_output[1]["decoder_head_matrices"]["k_matrices_tobesupervised"]
            elif self.supervised_matrix == "V":
                supervised_matrices_layers = net_output[1]["decoder_head_matrices"]["v_matrices_tobesupervised"]
            elif self.supervised_matrix == "attn_matrix":
                supervised_matrices_layers = net_output[1]["decoder_head_matrices"]["attention_matrices_tobesupervised"]
            elif self.supervised_matrix == "headout":
                supervised_matrices_layers = net_output[1]["decoder_head_matrices"]["headout_matrices_tobesupervised"]
            
            for cluster_centers, head_labels, supervised_matrices in zip(cluster_centers_layers, head_labels_layers, supervised_matrices_layers):
                assert cluster_centers[0].shape == supervised_matrices[0].shape
                assert head_labels.shape[0] == supervised_matrices.shape[0]

                num_heads = head_labels.shape[0]
                if self.cfg.use_inclass_loss:
                    for i in range(num_heads):
                        head_label = int(head_labels[i])
                        # supervised_matrices[i] - cluster_centers[head_label]
                        
                        Regu = Regu-(                                                      # TODO here we can use MSE or dotproduct to evaluate the distance, depending on the performance
                                    (
                                    supervised_matrices[i] @ cluster_centers[head_label] 
                                    /
                                    (LA.vector_norm(supervised_matrices[i]) * LA.vector_norm(cluster_centers[head_label]))
                                    ) 
                                    / num_heads
                                    ) * self.cluster_loss_coefficient_inclass                                       # TODO the normalization method is to be reconsidered
                if self.cfg.use_interclass_loss:
                    for i in range(cluster_centers.shape[0]):
                        for j in range(cluster_centers.shape[0]):
                            if i < j:
                                Regu = Regu+(                                                      # TODO here we can use MSE or dotproduct to evaluate the distance, depending on the performance
                                            (
                                            cluster_centers[i] @ cluster_centers[j] 
                                            /
                                            (LA.vector_norm(cluster_centers[i]) * LA.vector_norm(cluster_centers[j]))
                                            ) 
                                            / math.comb(cluster_centers.shape[0], 2)    # TODO check normalization
                                            ) * self.cluster_loss_coefficient_interclass
                                                                                    
                    
        return Regu, Regu-Regu_init


    def forward(self, model, sample, reduce=True):
        """Compute the loss for the given sample.

        Returns a tuple with three elements:
        1) the loss
        2) the sample size, which is used as the denominator for the gradient
        3) logging outputs to display while training
        """

        net_output = model(**sample["net_input"])
        loss, nll_loss = self.compute_loss(model, net_output, sample, reduce=reduce)

        
        # print("model.encoder.layers[0].self_attn.prune_indicator: ", model.encoder.layers[0].self_attn.prune_indicator, "model.cfg.keep_updating_cluster: ", model.cfg.keep_updating_cluster)
        
        
        assert type(model.encoder.layers[0].self_attn.prune_indicator) == type(model.decoder.layers[0].self_attn.prune_indicator)
        
        if (torch.equal(model.encoder.layers[0].self_attn.prune_indicator, 
                    torch.Tensor([-1]).repeat(model.encoder.layers[0].self_attn.num_heads).to(torch.device(model.encoder.layers[0].self_attn.prune_indicator.device))) 
            or model.cfg.keep_updating_cluster):
            loss, Cluster_loss = self.add_cluster_loss(net_output, loss)
        else:
            Cluster_loss = torch.tensor(0)
        
        sample_size = (
            sample["target"].size(0) if self.sentence_avg else sample["ntokens"]
        )
        logging_output = {
            "loss": loss.data,
            "cluster_loss": Cluster_loss.data,
            "nll_loss": nll_loss.data,
            "ntokens": sample["ntokens"],
            "nsentences": sample["target"].size(0),
            "sample_size": sample_size,
        }
        if self.report_accuracy:
            n_correct, total = self.compute_accuracy(model, net_output, sample)
            logging_output["n_correct"] = utils.item(n_correct.data)
            logging_output["total"] = utils.item(total.data)
        return loss, sample_size, logging_output

    def get_lprobs_and_target(self, model, net_output, sample):
        lprobs = model.get_normalized_probs(net_output, log_probs=True)
        target = model.get_targets(sample, net_output)
        if self.ignore_prefix_size > 0:
            if getattr(lprobs, "batch_first", False):
                lprobs = lprobs[:, self.ignore_prefix_size :, :].contiguous()
                target = target[:, self.ignore_prefix_size :].contiguous()
            else:
                lprobs = lprobs[self.ignore_prefix_size :, :, :].contiguous()
                target = target[self.ignore_prefix_size :, :].contiguous()
        return lprobs.view(-1, lprobs.size(-1)), target.view(-1)

    def compute_loss(self, model, net_output, sample, reduce=True):
        lprobs, target = self.get_lprobs_and_target(model, net_output, sample)
        loss, nll_loss = label_smoothed_nll_loss(
            lprobs,
            target,
            self.eps,
            ignore_index=self.padding_idx,
            reduce=reduce,
        )
        return loss, nll_loss

    def compute_accuracy(self, model, net_output, sample):
        lprobs, target = self.get_lprobs_and_target(model, net_output, sample)
        mask = target.ne(self.padding_idx)
        n_correct = torch.sum(
            lprobs.argmax(1).masked_select(mask).eq(target.masked_select(mask))
        )
        total = torch.sum(mask)
        return n_correct, total

    @classmethod
    def reduce_metrics(cls, logging_outputs) -> None:
        """Aggregate logging outputs from data parallel training."""
        loss_sum = sum(log.get("loss", 0) for log in logging_outputs)
        nll_loss_sum = sum(log.get("nll_loss", 0) for log in logging_outputs)
        ntokens = sum(log.get("ntokens", 0) for log in logging_outputs)
        sample_size = sum(log.get("sample_size", 0) for log in logging_outputs)
        cluster_loss_sum = sum(log.get("cluster_loss", 0) for log in logging_outputs)

        metrics.log_scalar(
            "loss", loss_sum / sample_size / math.log(2), sample_size, round=3
        )
        metrics.log_scalar(
            "nll_loss", nll_loss_sum / ntokens / math.log(2), ntokens, round=3
        )
        metrics.log_scalar(
            "cluster_loss", cluster_loss_sum / sample_size / math.log(2), sample_size, round=9
        )
        metrics.log_derived(
            "ppl", lambda meters: utils.get_perplexity(meters["nll_loss"].avg)
        )

        total = utils.item(sum(log.get("total", 0) for log in logging_outputs))
        if total > 0:
            metrics.log_scalar("total", total)
            n_correct = utils.item(
                sum(log.get("n_correct", 0) for log in logging_outputs)
            )
            metrics.log_scalar("n_correct", n_correct)
            metrics.log_derived(
                "accuracy",
                lambda meters: round(
                    meters["n_correct"].sum * 100.0 / meters["total"].sum, 3
                )
                if meters["total"].sum > 0
                else float("nan"),
            )

    @staticmethod
    def logging_outputs_can_be_summed() -> bool:
        """
        Whether the logging outputs returned by `forward` can be summed
        across workers prior to calling `reduce_metrics`. Setting this
        to True will improves distributed training speed.
        """
        return True
