import random
import pickle
import time
import logging
import os
from datetime import datetime
import pandas as pd

import torch
import torch.nn as nn
from torch.utils.data import DataLoader
from torch.optim import Adam
from sklearn.preprocessing import LabelEncoder

from config import Config
from preprocess import load_dataset, preprocess_df, create_datasets
from Model import CAMP
from training_utils import train, evaluate, test

random.seed(42) 

def setup_logging():
    logging.basicConfig(filename='../../log.txt', level=logging.DEBUG, format='%(asctime)s:%(levelname)s:%(message)s')

def load_data():
    dataset_name = 'sampled_Home_and_Kitchen'
    dataset_path = f'../../dataset/{dataset_name}/'
    review_file_path = f'{dataset_path}{dataset_name}.pkl'
    meta_file_path = f'{dataset_path}meta_{dataset_name}.pkl'
    processed_path = f'../../dataset/preprocessed/{dataset_name}/'

    if os.path.exists(f'{processed_path}/train_df.pkl') and os.path.exists(f'{processed_path}/valid_df.pkl') and os.path.exists(f'{processed_path}/test_df.pkl') and Config.data_preprocessed:
        with open(f'{processed_path}/train_df.pkl', 'rb') as file:
            train_df = pickle.load(file)
        with open(f'{processed_path}/valid_df.pkl', 'rb') as file:
            valid_df = pickle.load(file)
        with open(f'{processed_path}/test_df.pkl', 'rb') as file:
            test_df = pickle.load(file)
        
        combined_df = pd.concat([train_df, valid_df, test_df])
        num_users = combined_df['user_encoded'].nunique()
        num_items = combined_df['item_encoded'].nunique()
        num_cats = combined_df['cat_encoded'].nunique()
        print("Processed files already exist. Skipping dataset preparation.")
    else:
        try:
            start_time = time.time()
            df = load_dataset(review_file_path)
            df_meta = load_dataset(meta_file_path, meta=True)
            df = pd.merge(df, df_meta, on='item_id', how='left').drop_duplicates(subset=['item_id'])

            num_users = df['user_id'].nunique()
            num_items = df['item_id'].nunique()
            num_cats = df['category'].nunique()

            train_df, valid_df, test_df = preprocess_df(df, Config.time_range, Config.k_m, Config.k_s)

            if not os.path.exists(processed_path):
                os.makedirs(processed_path)
            date_str = datetime.now().strftime('%Y%m%d')
            train_df.to_pickle(f'{processed_path}/train_df_{date_str}_{num_users}_{num_items}.pkl')
            valid_df.to_pickle(f'{processed_path}/valid_df_{date_str}_{num_users}_{num_items}.pkl')
            test_df.to_pickle(f'{processed_path}/test_df_{date_str}_{num_users}_{num_items}.pkl')

            end_time = time.time()
            logging.info(f"Dataset prepared in {end_time - start_time:.2f} seconds")
        except Exception as e:
            logging.error(f"Error during data preparation: {str(e)}")
            raise

    return train_df, valid_df, test_df, num_users, num_items, num_cats

def main():
    print("Data Loading......")
    setup_logging()
    train_df, valid_df, test_df, num_users, num_items, num_cats = load_data()

    train_dataset, valid_dataset, test_dataset = create_datasets(train_df, valid_df, test_df)
    train_loader = DataLoader(train_dataset, batch_size=Config.batch_size, shuffle=True, drop_last=True)
    valid_loader = DataLoader(valid_dataset, batch_size=Config.batch_size)
    test_loader = DataLoader(test_dataset, batch_size=Config.batch_size)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    model = CAMP(num_users, num_items, num_cats, Config.embedding_dim, Config.hidden_dim, Config.output_dim).to(device)
    if torch.cuda.device_count() > 1:
        print(f"Let's use {torch.cuda.device_count()} GPUs!")
        model = nn.DataParallel(model)
        
    # Define the optimizer
    optimizer = Adam(model.parameters(), lr=0.001)

    print("Training......")
    # Train and evaluate
    for epoch in range(Config.num_epochs):
        train_loss = train(model, train_loader, optimizer, device)
        valid_loss = evaluate(model, valid_loader, device)
        logging.info(f'Epoch {epoch+1}, Train Loss: {train_loss}, Valid Loss: {valid_loss}')

    save_path = "../../model/"
    os.makedirs(save_path, exist_ok=True)
    model_filename = f"trained_model_{datetime.now().strftime('%Y-%m-%d')}.pt"
    full_path = os.path.join(save_path, model_filename)
    torch.save({
        'model_state_dict': model.state_dict(),
        'optimizer_state_dict': optimizer.state_dict(),
        'train_loss': train_loss,
        'valid_loss': valid_loss,
        'epoch': epoch,
    }, full_path)
    logging.info(f"Model and training states have been saved to {full_path}")

    # Evaluate on test set
    average_loss, all_top_k_items, avg_precision, avg_recall, avg_ndcg, avg_hit_rate, avg_auc, avg_mrr = test(model, test_loader, device)

if __name__ == "__main__":
    main()
