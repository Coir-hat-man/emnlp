import copy
import random

# typing
from typing import List, Tuple
import time
import torch
import itertools

# TODO
# from transformers import LlamaTokenizer
# tokenizer=LlamaTokenizer.from_pretrained("/home/lyh/weights/hf/vicuna_v13/7B/")

TOPK = 10  # topk for sparse tree

from transformers.generation.logits_process import (
    LogitsProcessorList,
    RepetitionPenaltyLogitsProcessor,
    TemperatureLogitsWarper,
    TopKLogitsWarper,
    TopPLogitsWarper,
)


class TensorCompressor:
    def __init__(self, input_tensor, draft_qlen):
        self.draft_qlen = draft_qlen
        self.bsz, self.q_len,self.hidden_size = input_tensor.size()
        self.device = input_tensor.device
        self.dtype = input_tensor.dtype
        self.cumulative_lengths = np.cumsum([0] + draft_qlen)
        self.total_valid = self.cumulative_lengths[-1]

    def compress(self, input_tensor):
        compressed = torch.zeros(
            (self.total_valid, self.hidden_size),
            device=self.device,
            dtype=self.dtype
        )
        for i in range(self.bsz):
            valid_len = self.draft_qlen[i]
            if valid_len > 0:
                start = self.cumulative_lengths[i]
                end = self.cumulative_lengths[i+1]
                compressed[start:end] = input_tensor[i, :valid_len]
        return compressed

    def restore(self, input_tensor):
        output = torch.zeros(
            (self.bsz * self.q_len, self.hidden_size),
            device=self.device,
            dtype=self.dtype
        )
        for i in range(self.bsz):
            valid_len = self.draft_qlen[i]
            if valid_len > 0:
                start = self.cumulative_lengths[i]
                end = self.cumulative_lengths[i+1]
                output[i*self.q_len : i*self.q_len+valid_len] = input_tensor[start:end]
        return output


def prepare_logits_processor(
        temperature: float = 0.0,
        repetition_penalty: float = 0.0,
        top_p: float = 0.0,
        top_k: int = 0
) -> LogitsProcessorList:
    processor_list = LogitsProcessorList()
    if temperature > 1e-5:
        if temperature >= 1e-5 and temperature != 1.0:
            processor_list.append(TemperatureLogitsWarper(temperature))
        if repetition_penalty > 1.0:
            processor_list.append(RepetitionPenaltyLogitsProcessor(repetition_penalty))
        if 1e-8 <= top_p < 1.0:
            processor_list.append(TopPLogitsWarper(top_p))
        if top_k > 0:
            processor_list.append(TopKLogitsWarper(top_k))
        return processor_list


# test_processor = prepare_logits_processor(
#         0.0, 0.0, -1, 1
#     )


def pad_path(path: List[int], length: int, pad_value: int = -2) -> List[int]:
    """
    Pad the given path list with a specific value up to a specified length.

    Parameters:
    - path (list): The original list that needs padding.
    - length (int): The desired length of the padded list.
    - pad_value (optional, default=-2): The value to use for padding.

    Returns:
    - list: A new list based on the original path but padded to the desired length.

    Example:
    >>> pad_path([1,2,3], 5)
    [1, 2, 3, -2, -2]

    Note:
    If the given path is already longer than the specified length,
    then no padding occurs, and the original path is returned.
    """

    # Calculate the number of padding values needed by subtracting the length
    # of the path from the desired length.
    # Append the padding values to the original path and return the new list.
    return path + [pad_value] * (length - len(path))


def generate_tree_buffers(tree_choices, device="cuda"):
    sorted_tree_choices = sorted(tree_choices, key=lambda x: (len(x), x))
    tree_len = len(sorted_tree_choices) + 1

    # Initialize depth_counts to keep track of how many choices have a particular depth
    depth_counts = []
    prev_depth = 0
    for path in sorted_tree_choices:
        depth = len(path)
        if depth != prev_depth:
            depth_counts.append(0)
        depth_counts[depth - 1] += 1
        prev_depth = depth

    tree_attn_mask = torch.eye(tree_len, tree_len)
    tree_attn_mask[:, 0] = 1
    start = 0
    for i in range(len(depth_counts)):
        for j in range(depth_counts[i]):
            cur_tree_choice = sorted_tree_choices[start + j]
            # retrieve ancestor position
            if len(cur_tree_choice) == 1:
                continue
            ancestor_idx = []
            for c in range(len(cur_tree_choice) - 1):
                ancestor_idx.append(sorted_tree_choices.index(cur_tree_choice[:c + 1]) + 1)
            tree_attn_mask[j + start + 1, ancestor_idx] = 1
        start += depth_counts[i]

    tree_indices = torch.zeros(tree_len, dtype=torch.long)
    p_indices = [0 for _ in range(tree_len - 1)]
    b_indices = [[] for _ in range(tree_len - 1)]
    tree_indices[0] = 0
    start = 0
    bias = 0
    for i in range(len(depth_counts)):
        inlayer_bias = 0
        b = []
        for j in range(depth_counts[i]):
            cur_tree_choice = sorted_tree_choices[start + j]
            cur_parent = cur_tree_choice[:-1]
            if j != 0:
                if cur_parent != parent:
                    bias += 1
                    inlayer_bias += 1
                    parent = cur_parent
                    b = []
            else:
                parent = cur_parent
            tree_indices[start + j + 1] = cur_tree_choice[-1] + TOPK * (i + bias) + 1
            p_indices[start + j] = inlayer_bias
            if len(b) > 0:
                b_indices[start + j] = copy.deepcopy(b)
            else:
                b_indices[start + j] = []
            b.append(cur_tree_choice[-1] + TOPK * (i + bias) + 1)
        start += depth_counts[i]

    p_indices = [-1] + p_indices
    tree_position_ids = torch.zeros(tree_len, dtype=torch.long)
    start = 0
    for i in range(len(depth_counts)):
        tree_position_ids[start + 1: start + depth_counts[i] + 1] = i + 1
        start += depth_counts[i]

    retrieve_indices_nest = []
    retrieve_paths = []
    for i in range(len(sorted_tree_choices)):
        cur_tree_choice = sorted_tree_choices[-i - 1]
        retrieve_indice = []
        if cur_tree_choice in retrieve_paths:
            continue
        else:
            for c in range(len(cur_tree_choice)):
                retrieve_indice.append(sorted_tree_choices.index(cur_tree_choice[:c + 1]))
                retrieve_paths.append(cur_tree_choice[:c + 1])
        retrieve_indices_nest.append(retrieve_indice)
    max_length = max([len(x) for x in retrieve_indices_nest])
    retrieve_indices = [pad_path(path, max_length) for path in retrieve_indices_nest]
    retrieve_indices = torch.tensor(retrieve_indices, dtype=torch.long)
    retrieve_indices = retrieve_indices + 1
    retrieve_indices = torch.cat([torch.zeros((retrieve_indices.shape[0], 1), dtype=torch.long), retrieve_indices],
                                 dim=1)

    maxitem = retrieve_indices.max().item() + 5

    def custom_sort(lst):
        # sort_keys=[len(list)]
        sort_keys = []
        for i in range(len(lst)):
            sort_keys.append(lst[i] if lst[i] >= 0 else maxitem)
        return sort_keys

    retrieve_indices = retrieve_indices.tolist()
    retrieve_indices = sorted(retrieve_indices, key=custom_sort)
    retrieve_indices = torch.tensor(retrieve_indices, dtype=torch.long)

    p_indices = torch.tensor(p_indices)
    p_indices_new = p_indices[retrieve_indices]
    p_indices_new = p_indices_new.tolist()

    b_indices = [[]] + b_indices
    b_indices_new = []
    for ib in range(retrieve_indices.shape[0]):
        iblist = []
        for jb in range(retrieve_indices.shape[1]):
            index = retrieve_indices[ib, jb]
            if index == -1:
                iblist.append([])
            else:
                b = b_indices[index]
                if len(b) > 0:
                    bt = []
                    for bi in b:
                        bt.append(torch.where(tree_indices == bi)[0].item())
                    iblist.append(torch.tensor(bt, device=device))
                else:
                    iblist.append(b)
        b_indices_new.append(iblist)

    # Aggregate the generated buffers into a dictionary
    tree_buffers = {
        "tree_attn_mask": tree_attn_mask.unsqueeze(0).unsqueeze(0),
        "tree_indices": tree_indices,
        "tree_position_ids": tree_position_ids,
        "retrieve_indices": retrieve_indices,
    }

    # Move the tensors in the dictionary to the specified device
    tree_buffers = {
        k: v.clone().to(device)
        if isinstance(v, torch.Tensor)
        else torch.tensor(v, device=device)
        for k, v in tree_buffers.items()
    }
    tree_buffers["p_indices"] = p_indices_new
    tree_buffers["b_indices"] = b_indices_new
    return tree_buffers


def initialize_tree(input_ids, model, tree_attn_mask, past_key_values, attention_mask=None, tree_buffers=None):
    position_ids = attention_mask.long().cumsum(-1) - 1
    # print(f"attention_mask: {attention_mask}")
    # print(f"before position_ids: {position_ids}")
    position_ids.masked_fill_(attention_mask == 0, 1)
    # print(f"after position_ids: {position_ids}")
    tree_logits, outputs, logits, hidden_state, sample_token = model(
        input_ids, past_key_values=past_key_values, output_orig=True,
        attention_mask=attention_mask, position_ids=position_ids, tree_buffers=tree_buffers, exec_type="prefill"
    )
    return tree_logits, logits, hidden_state, sample_token


# def reset_past_key_values(passed_key_values: List[torch.Tensor]) -> List[torch.Tensor]:
#     """
#     Resets the current lengths in the passed key-values to zero.

#     This function is designed to be used during the evaluation of a baseline model.
#     It iterates through each layer's key-values and sets their current lengths to zero,
#     effectively resetting their state.

#     Args:
#     - passed_key_values (list of torch.Tensor): Contains past hidden states and past attention values for each layer.

#     Returns:
#     - passed_key_values (list of torch.Tensor): Updated past hidden states and past attention values with reset lengths.
#     """
#     for i in range(len(passed_key_values)):
#         for j in range(2):
#             passed_key_values[i][j].current_length.fill_(0)
#     return passed_key_values


def generate_candidates(tree_logits, tree_indices, retrieve_indices, sample_token, logits_processor):
    bs = sample_token.shape[0]
    sample_token = sample_token.to(tree_indices.device)

    # candidates_logit = sample_token[0]
    candidates_logit = sample_token

    candidates_tree_logits = tree_logits[0]

    candidates = torch.cat([candidates_logit, candidates_tree_logits.view(bs, -1)], dim=-1)

    tree_candidates = candidates[:, tree_indices]

    tree_candidates_ext = torch.cat(
        [tree_candidates, torch.zeros((bs, 1), dtype=torch.long, device=tree_candidates.device)-1], dim=-1)

    cart_candidates = tree_candidates_ext[:, retrieve_indices]

    if logits_processor is not None:
        candidates_tree_prob = tree_logits[1]
        candidates_prob = torch.cat(
            [torch.ones((bs, 1), device=candidates_tree_prob.device, dtype=torch.float32),
             candidates_tree_prob.view(bs, -1)],
            dim=-1)

        tree_candidates_prob = candidates_prob[:, tree_indices]
        tree_candidates_prob_ext = torch.cat(
            [tree_candidates_prob, torch.ones((bs, 1), dtype=torch.float32, device=tree_candidates_prob.device)],
            dim=-1)
        cart_candidates_prob = tree_candidates_prob_ext[:, retrieve_indices]
    else:
        cart_candidates_prob = None
    # Unsqueeze the tree candidates for dimension consistency.

    #
    # tree_candidates = tree_candidates.unsqueeze(0)
    return cart_candidates, cart_candidates_prob, tree_candidates


def tree_decoding(
        model,
        tree_candidates,
        past_key_values,
        tree_position_ids,
        input_ids,
        retrieve_indices,
        attention_mask=None,
        cache_lens = None,
        tree_buffers = None
):
    zero_num = attention_mask.shape[1] - attention_mask.long().sum(-1)
    zero_num = zero_num[:, None]
    position_ids = tree_position_ids[None, :] + input_ids.shape[1] - zero_num
    # print(f"attention_mask: {attention_mask}")

    attention_mask = torch.cat(
        (attention_mask, torch.ones_like(tree_candidates, device=attention_mask.device, dtype=attention_mask.dtype)),
        dim=1)

    outputs, tree_logits, hidden_state = model(
        tree_candidates,
        attention_mask=attention_mask,
        output_orig=True,
        past_key_values=past_key_values,
        position_ids=position_ids,
        init=False,
        cache_lens = cache_lens,
        tree_buffers = tree_buffers,
        exec_type="tree_decoding"
    )

    logits = tree_logits[:, retrieve_indices]
    return logits, hidden_state, outputs


def evaluate_posterior(
        logits, candidates, logits_processor, cart_candidates_prob, op, p_indices, tree_candidates, b_indices,
        finish_flag
):
    """
    Evaluate the posterior probabilities of the candidates based on the provided logits and choose the best candidate.

    Depending on the temperature value, the function either uses greedy decoding or evaluates posterior
    probabilities to select the best candidate.

    Args:
    - logits (torch.Tensor): Predicted logits of shape (batch_size, sequence_length, vocab_size).
    - candidates (torch.Tensor): Candidate token sequences.
    - temperature (float): Softmax temperature for probability scaling. A value of 0 indicates greedy decoding.
    - posterior_threshold (float): Threshold for posterior probability.
    - posterior_alpha (float): Scaling factor for the threshold.

    Returns:
    - best_candidate (torch.Tensor): Index of the chosen best candidate.
    - accept_length (int): Length of the accepted candidate sequence.
    """
    # Greedy decoding based on temperature value
    if logits_processor is None:
        bs = tree_candidates.size(0)
        # Find the tokens that match the maximum logits for each position in the sequence
        posterior_mask = (
                candidates[:, :, 1:].to(logits.device) == torch.argmax(logits[:, :, :-1], dim=-1)
        ).int()
        candidates_accept_length = (torch.cumprod(posterior_mask, dim=-1)).sum(dim=-1)
        accept_length = candidates_accept_length.max(dim=1).values
        best_candidate = torch.argmax(candidates_accept_length, dim=-1).to(torch.long)


        bt = tuple(range(bs))
        logits_batch = logits[bt, best_candidate, accept_length, :]
        accept_length = accept_length.tolist()

        for batch in range(bs):
            if finish_flag[batch]:
                accept_length[batch] = 0

        return best_candidate.tolist(), accept_length, logits_batch

    else:
        cart_candidates_prob = cart_candidates_prob.to(logits.device)
        bs = cart_candidates_prob.size(0)

        logits = logits_processor(None, logits)
        probs = torch.softmax(logits, dim=-1)

        best_candidate_list = []
        accept_length_list = []
        sample_p_list = []

        for batch in range(bs):
            accept_length = 1
            accept_cand = candidates[batch, 0, :1]
            best_candidate = 0
            for i in range(1, candidates.shape[2]):
                if i != accept_length:
                    break
                adjustflag = False
                is_eq = (candidates[batch, :, :accept_length] == accept_cand).all(dim=1)
                fi = torch.nonzero(is_eq, as_tuple=True)[0][0]
                gtp = probs[batch, fi, i - 1]
                candidates_set = []
                for j in range(candidates.shape[1]):
                    if is_eq[j]:
                        x = candidates[batch, j, i]
                        xi = x.item()
                        if xi in candidates_set or xi == -1:
                            continue
                        candidates_set.append(xi)
                        r = random.random()
                        px = gtp[xi]
                        qx = cart_candidates_prob[batch, j, i]
                        if qx <= 0:
                            continue
                        acp = px / qx
                        if r <= acp:
                            accept_cand = torch.cat((accept_cand, x[None]), dim=0)
                            accept_length += 1
                            best_candidate = j
                            break
                        else:
                            q = op[i - 1][batch][p_indices[j][i]].clone()
                            b = b_indices[j][i]
                            if len(b) > 0:
                                mask = tree_candidates[batch][b]
                                q[mask] = 0
                                q = q / q.sum()
                            gtp = gtp - q
                            gtp[gtp < 0] = 0
                            gtp = gtp / gtp.sum()
                            adjustflag = True
            if adjustflag and accept_length != candidates.shape[1]:
                sample_p = gtp
            else:
                sample_p = probs[batch, best_candidate, accept_length - 1]
            best_candidate_list.append(best_candidate)
            accept_length_list.append(accept_length - 1)
            sample_p_list.append(sample_p)

        for batch in range(bs):
            if finish_flag[batch]:
                accept_length_list[batch] = 0

        return best_candidate_list, accept_length_list, sample_p_list
