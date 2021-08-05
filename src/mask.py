#!/usr/bin/env python3
# Author: Joel Ye

import torch
import torch.nn as nn
import torch.nn.functional as F
import pdb

# Some infeasibly high spike count
DEFAULT_MASK_VAL = 30
UNMASKED_LABEL = -100
SUPPORTED_MODES = ["full", "timestep", "neuron", "timestep_only"]

# Use a class so we can cache random mask
class Masker:

    def __init__(self, train_cfg, device):
        self.update_config(train_cfg)
        if self.cfg.MASK_MODE not in SUPPORTED_MODES:
            raise Exception(f"Given {self.cfg.MASK_MODE} not in supported {SUPPORTED_MODES}")
        self.device = device

    def update_config(self, config):
        self.cfg = config
        self.prob_mask = None

    def expand_mask(self, mask, width):
        r"""
            args:
                mask: N x T
                width: expansion block size
        """
        kernel = torch.ones(width, device=mask.device).view(1, 1, -1)
        expanded_mask = F.conv1d(mask.unsqueeze(1), kernel, padding= width// 2).clamp_(0, 1)
        if width % 2 == 0:
            expanded_mask = expanded_mask[...,:-1] # crop if even (we've added too much padding)
        return expanded_mask.squeeze(1)

    def mask_batch(
        self,
        batch,
        mask=None,
        max_spikes=DEFAULT_MASK_VAL - 1,
        should_mask=True,
        expand_prob=0.0,
    ):
        r""" Given complete batch, mask random elements and return true labels separately.
        Modifies batch OUT OF place!
        Modeled after HuggingFace's `mask_tokens` in `run_language_modeling.py`
        args:
            batch: batch NxTxH
            mask_ratio: ratio to randomly mask
            mode: "full" or "timestep" - if "full", will randomly drop on full matrix, whereas on "timestep", will mask out random timesteps
            mask: Optional mask to use
            max_spikes: in case not zero masking, "mask token"
            expand_prob: with this prob, uniformly expand. else, keep single tokens. UniLM does, with 40% expand to fixed, else keep single.
        returns:
            batch: list of data batches NxTxH, with some elements along H set to -1s (we allow peeking between rates)
            labels: true data (also NxTxH)
        """
        batch = batch.clone() # make sure we don't corrupt the input data (which is stored in memory)

        # obtain nan mask
        nan_mask = torch.isnan(batch)
        
        mode = self.cfg.MASK_MODE
        should_expand = self.cfg.MASK_MAX_SPAN > 1 and expand_prob > 0.0 and torch.rand(1).item() < expand_prob
        width =  torch.randint(1, self.cfg.MASK_MAX_SPAN + 1, (1, )).item() if should_expand else 1
        mask_ratio = self.cfg.MASK_RATIO if width == 1 else self.cfg.MASK_RATIO / width

        labels = batch.clone()
        
        if mask is None:
            if self.prob_mask is None or self.prob_mask.size() != labels.size():
                if mode == "full":
                    mask_probs = torch.full(labels.shape, mask_ratio)
                elif mode == "timestep":
                    single_timestep = labels[:, :, 0] # N x T
                    mask_probs = torch.full(single_timestep.shape, mask_ratio)
                elif mode == "neuron":
                    single_neuron = labels[:, 0] # N x H
                    mask_probs = torch.full(single_neuron.shape, mask_ratio)
                elif mode == "timestep_only":
                    single_timestep = labels[0, :, 0] # T
                    mask_probs = torch.full(single_timestep.shape, mask_ratio)
                self.prob_mask = mask_probs.to(self.device)
            # If we want any tokens to not get masked, do it here (but we don't currently have any)
            mask = torch.bernoulli(self.prob_mask)

            # N x T
            if width > 1:
                mask = self.expand_mask(mask, width)

            mask = mask.bool()
            if mode == "timestep":
                mask = mask.unsqueeze(2).expand_as(labels)
            elif mode == "neuron":
                mask = mask.unsqueeze(0).expand_as(labels)
            elif mode == "timestep_only":
                mask = mask.unsqueeze(0).unsqueeze(2).expand_as(labels)
                # we want the shape of the mask to be T
        elif mask.size() != labels.size():
            raise Exception(f"Input mask of size {mask.size()} does not match input size {labels.size()}")

        # combine ndt mask with nan mask and label unmasked
        labels[~mask + nan_mask] = UNMASKED_LABEL
        #labels[~mask] = UNMASKED_LABEL  # No ground truth for unmasked - use this to mask loss
        
        if not should_mask:
            # Only do the generation
            return batch, labels

        # We use random assignment so the model learns embeddings for non-mask tokens, and must rely on context
        # Most times, we replace tokens with MASK token
        indices_replaced = torch.bernoulli(torch.full(labels.shape, self.cfg.MASK_TOKEN_RATIO, device=mask.device)).bool() & mask

        if self.cfg.USE_ZERO_MASK:
            # combine ndt mask and nan mask and zero-filled masked samples
            batch[indices_replaced + nan_mask] = 0
            #batch[indices_replaced] = 0
        else:
            batch[indices_replaced] = max_spikes + 1

        # Random % of the time, we replace masked input tokens with random value (the rest are left intact)
        indices_random = torch.bernoulli(torch.full(labels.shape, self.cfg.MASK_RANDOM_RATIO, device=mask.device)).bool() & mask & ~indices_replaced
        
        # deal with data type
        random_spikes = torch.randint(batch.max().long(), labels.shape, dtype=torch.long, device=batch.device)
        batch[indices_random] = random_spikes.float()[indices_random]
        # original implementation
        #random_spikes = torch.randint(batch.max(), labels.shape, dtype=torch.long, device=batch.device) # modified by FZ 08/04/2021
        #batch[indices_random] = random_spikes[indices_random] # modified by FZ 08/04/2021

        # Leave the other 10% alone
        return batch, labels
