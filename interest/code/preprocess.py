import random
import pandas as pd
import numpy as np
import pickle
from functools import lru_cache
import torch
from torch.utils.data import Dataset

from tqdm.auto import tqdm
from concurrent.futures import ThreadPoolExecutor, as_completed
from multiprocessing import cpu_count

tqdm.pandas()

def load_dataset(file_path, type = 'pop'):
    with open(file_path, 'rb') as file:
        df = pickle.load(file) 
    if type == 'meta':
        necessary_columns = ['average_rating', 'rating_number', 'store', 'parent_asin', 'categories']
        df = df[necessary_columns].rename(columns={'average_rating': 'avg_rating', 'parent_asin': 'item_id'})
        df['category'] = df['categories'].apply(lambda x: x[1] if len(x) > 1 else (x[0] if len(x) == 1 else None))
    elif type == 'review':
        df = df.rename(columns={'parent_asin': 'item_id'})                  
    return df

def encode_column(column, pad = False):
    frequencies = column.value_counts(ascending=False)
    if pad:
        mapping = pd.Series(index=frequencies.index, data=range(1, len(frequencies) + 1))
    else:
        mapping = pd.Series(index=frequencies.index, data=range(len(frequencies)))
    encoded_column = column.map(mapping).fillna(0).astype(int)
    return encoded_column

def get_history(group):
    group_array = np.array(group)
    histories = []
    for i in range(len(group_array)):
        history = group_array[max(0, i - 128 + 1):i + 1]  
        histories.append(np.pad(history, (128 - len(history), 0), mode='constant'))    
    return histories

def calculate_ranges(group, k_m, k_s):
    if not pd.api.types.is_datetime64_any_dtype(group['timestamp']):
        group['timestamp'] = pd.to_datetime(group['timestamp'], unit='ms')
    k_m_delta = pd.Timedelta(milliseconds=k_m)
    k_s_delta = pd.Timedelta(milliseconds=k_s)

    group.set_index('timestamp', inplace=True)
    group['mid_len'] = group.index.to_series().apply(lambda x: group.loc[x-k_m_delta:x].shape[0] - 1)
    group['short_len'] = group.index.to_series().apply(lambda x: group.loc[x-k_s_delta:x].shape[0] - 1)
    group.reset_index(inplace=True)

    return group[['mid_len', 'short_len']]

@lru_cache(maxsize=1024)
def generate_negative_samples_cached(all_item_ids, positive_item_id, history_tuple, num_samples=4):
    history = set(history_tuple)
    non_interacted_items = list(all_item_ids - history - {positive_item_id})
    if len(non_interacted_items) >= num_samples:
        neg_samples = random.sample(non_interacted_items, num_samples)
    else:
        neg_samples = non_interacted_items
    return neg_samples

def generate_negative_samples_for_row(all_item_ids, item_encoded, item_his_encoded, num_samples):
    return generate_negative_samples_cached(
        frozenset(all_item_ids),
        item_encoded,
        tuple(item_his_encoded),
        num_samples
    )

def generate_negative_samples_vectorized(df, all_item_ids, num_samples):
    all_item_ids = np.array(list(all_item_ids))

    # Create an array for negative samples
    neg_samples = np.zeros((len(df), num_samples), dtype=np.int32)

    # Get the positive items and their histories
    positive_items = df['item_encoded'].values
    histories = df['item_his_encoded_set'].values

    # Vectorized negative sampling
    for idx in tqdm(range(len(df)), desc="Generating negative samples"):
        item_his_set = histories[idx]
        positive_item_id = positive_items[idx]

        # Get the non-interacted items
        mask = np.isin(all_item_ids, list(item_his_set) + [positive_item_id], invert=True)
        non_interacted_items = all_item_ids[mask]

        # Sample negative items
        if len(non_interacted_items) >= num_samples:
            sampled_items = np.random.choice(non_interacted_items, num_samples, replace=False)
        else:
            sampled_items = non_interacted_items

        neg_samples[idx, :len(sampled_items)] = sampled_items

    return neg_samples.tolist()

def preprocess_df(df, config):     
    df['user_encoded'] = encode_column(df['user_id'])
    df['item_encoded'] = encode_column(df['item_id'], pad = True)
    df['cat_encoded'] = encode_column(df['category'], pad = True)
    item_to_cat_dict = dict(zip(df['item_encoded'], df['cat_encoded']))
    item_to_con_dict = df.groupby('item_encoded')['conformity'].last().to_dict()
    item_to_qlt_dict = df.groupby('item_encoded')['quality'].last().to_dict()

    max_item_id = df['item_encoded'].max()
    all_item_ids = set(range(1, max_item_id + 1))

    min_time_all = df['timestamp'].min()
    df['unit_time'] = (df['timestamp'] - min_time_all) // config.time_range

    df['item_his_encoded'] = df.groupby('user_id')['item_encoded'].transform(get_history) 
    df['cat_his_encoded'] = df.groupby('user_id')['cat_encoded'].transform(get_history) 
    df['con_his'] = df.groupby('user_id')['conformity'].transform(get_history) 
    df['qlt_his'] = df.groupby('user_id')['quality'].transform(get_history) 

    # Precompute item_his_encoded as sets and tuples
    df['item_his_encoded_set'] = df['item_his_encoded'].apply(set)
    df['item_his_encoded_tuple'] = df['item_his_encoded'].apply(tuple)

    print('calculate ranges start')
    ranges_df = df.groupby('user_id', group_keys=False).apply(lambda x: calculate_ranges(x, config.k_m, config.k_s), include_groups=False)
    df.reset_index(drop=True, inplace=True)
    ranges_df.reset_index(drop=True, inplace=True)
    df = pd.concat([df, ranges_df], axis=1) 
    print('calculate ranges end')

    df = df[['user_id', 'user_encoded', 'item_encoded', 'cat_encoded', 'conformity', 'quality', 'item_his_encoded', 'item_his_encoded_set', 'item_his_encoded_tuple', 'cat_his_encoded', 'con_his', 'qlt_his', 'unit_time', 'mid_len', 'short_len']]
    # df['item_his_encoded_set'] = df['item_his_encoded'].apply(set)
    # print("df:\n", df)
    
    print('Split into train, valid, test dataframe...')
    train_df = df.groupby('user_id').apply(lambda x: x.iloc[:-2], include_groups=False).reset_index(drop=True)
    valid_df = df.groupby('user_id').apply(lambda x: x.iloc[-2:-1], include_groups=False).reset_index(drop=True)
    test_df = df.groupby('user_id').apply(lambda x: x.iloc[-1:], include_groups=False).reset_index(drop=True)       

    print('Making negative sample start')    
    train_df['neg_items'] = generate_negative_samples_vectorized(train_df, all_item_ids, config.train_num_samples)
    valid_df['neg_items'] = generate_negative_samples_vectorized(valid_df, all_item_ids, config.valid_num_samples)
    test_df['neg_items'] = generate_negative_samples_vectorized(test_df, all_item_ids, config.test_num_samples)
    test_df['neg_items'] = test_df['neg_items'] + test_df['item_encoded'].apply(lambda x: [x])
    print('Making negative sample end')
    return train_df, valid_df, test_df, item_to_cat_dict, item_to_con_dict, item_to_qlt_dict

class MakeDataset(Dataset):
    def __init__(self, users, items, cats, cons, qlts, item_histories, cat_histories, con_histories, qlt_histories, mid_lens, short_lens, neg_items=None):
        self.users = torch.tensor(users, dtype=torch.long)
        self.items = torch.tensor(items, dtype=torch.long)  
        self.cats = torch.tensor(cats, dtype=torch.long)       
        self.cons = torch.tensor(cons, dtype=torch.float)  
        self.qlts = torch.tensor(qlts, dtype=torch.float) 
        self.item_histories = [torch.tensor(h, dtype=torch.long) for h in item_histories]
        self.cat_histories = [torch.tensor(c, dtype=torch.long) for c in cat_histories]
        self.con_histories = [torch.tensor(c, dtype=torch.float) for c in con_histories]
        self.qlt_histories = [torch.tensor(q, dtype=torch.float) for q in qlt_histories]
        self.mid_lens = torch.tensor(mid_lens, dtype=torch.int)
        self.short_lens = torch.tensor(short_lens, dtype=torch.int)
        # Only initialize can_neg if provided
        # if can_neg is not None:
        #     self.can_neg = [torch.tensor(n, dtype=torch.long) for n in can_neg]
        # else:
        #     self.can_neg = None
        if neg_items is not None:
            self.neg_items = [torch.tensor(n, dtype=torch.long) for n in neg_items]
        else:
            self.neg_items = None

    def __len__(self):
        return len(self.users)
    
    def __getitem__(self, idx):
        data = {
            'user': self.users[idx],
            'item': self.items[idx],   
            'cat': self.cats[idx],       
            'con': self.cons[idx],   
            'qlt': self.qlts[idx],      
            'item_his': self.item_histories[idx],
            'cat_his': self.cat_histories[idx],
            'con_his': self.con_histories[idx],
            'qlt_his': self.qlt_histories[idx],
            'mid_len': self.mid_lens[idx],
            'short_len': self.short_lens[idx]
        }
        # if self.can_neg is not None:
        #     data['can_neg'] = self.can_neg[idx]
        # return data
        if self.neg_items is not None:
            data['neg_items'] = self.neg_items[idx]
        return data

def create_datasets(train_df, valid_df, test_df):
    train_dataset = MakeDataset(
        train_df['user_encoded'], train_df['item_encoded'], train_df['cat_encoded'], train_df['conformity'], train_df['quality'],
        train_df['item_his_encoded'], train_df['cat_his_encoded'],  train_df['con_his'], train_df['qlt_his'], 
        train_df['mid_len'], train_df['short_len'], train_df['neg_items']
    )
    valid_dataset = MakeDataset(
        valid_df['user_encoded'], valid_df['item_encoded'], valid_df['cat_encoded'], valid_df['conformity'], valid_df['quality'],
        valid_df['item_his_encoded'], valid_df['cat_his_encoded'], valid_df['con_his'], valid_df['qlt_his'], 
        valid_df['mid_len'], valid_df['short_len'], valid_df['neg_items']
    )
    test_dataset = MakeDataset(
        test_df['user_encoded'], test_df['item_encoded'], test_df['cat_encoded'], test_df['conformity'], test_df['quality'],
        test_df['item_his_encoded'], test_df['cat_his_encoded'], test_df['con_his'], test_df['qlt_his'], 
        test_df['mid_len'], test_df['short_len'], test_df['neg_items']
    )
    return train_dataset, valid_dataset, test_dataset