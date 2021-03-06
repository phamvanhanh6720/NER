import os
import ast
from typing import Optional

import pandas as pd
import numpy as np
from typing import List

import torch
from sklearn.model_selection import train_test_split

from transformers import AutoTokenizer, MLukeTokenizer
from pytorch_lightning.core import LightningDataModule
from torch.utils.data import Dataset, DataLoader

from utils.dataset import NERDataSet


class CustomDataset(Dataset):
    def __init__(self,
                 dataset_path: str,
                 model_name_or_path: str,
                 tags_list: List[str],
                 max_seq_length: int = 128,
                 label_all_tokens: bool = False):

        self.max_seq_length = max_seq_length
        self.label_all_tokens = label_all_tokens

        if 'mluke' in model_name_or_path:
            self.tokenizer = MLukeTokenizer.from_pretrained(model_name_or_path, use_fast=True)
        else:
            self.tokenizer = AutoTokenizer.from_pretrained(model_name_or_path, use_fast=True)
        self.tag2id = {}
        for i in range(len(tags_list)):
            self.tag2id[tags_list[i]] = i

        self.dataset = []
        sen_dataset = self.read_data(dataset_path=dataset_path)
        for i in range(len(sen_dataset)):
            conll_format = sen_dataset[i]
            tokenized_inputs = self._tokenize_and_align_labels(data_point=conll_format)
            self.dataset.append(tokenized_inputs)

    @staticmethod
    def read_data(dataset_path: str) -> list:
        dataset = []
        with open(dataset_path, 'r') as file:
            lines = file.readlines()

        lines = [line.strip('\n').replace('\t', ' ').replace('B-MISCELLANEOUS', 'O').replace('I-MISCELLANEOUS', 'O')
                 for line in lines]
        break_idxs = list(np.where(np.array(lines) == '')[0])

        for i in range(len(break_idxs) - 1):
            start_idx = break_idxs[i]
            end_idx = break_idxs[i + 1]

            if start_idx != 0:
                start_idx += 1

            dataset.append(lines[start_idx: end_idx])

        return dataset

    def __len__(self):
        return len(self.dataset)

    def __getitem__(self, idx):
        item: dict = self.dataset[idx]

        return item

    def _tokenize_and_align_labels(self, data_point: List[str]):
        word_list = [word.split(' ')[0] for word in data_point]
        label_list = [word.split(' ')[-1] for word in data_point]

        text = ' '.join(word_list)
        tokenized_inputs = self.tokenizer(text,
                                          truncation=True,
                                          is_split_into_words=False,
                                          max_length=self.max_seq_length,
                                          padding='max_length')
        word_ids = tokenized_inputs.word_ids()

        previous_word_idx = None
        label_ids = []
        count = 0

        for word_idx in word_ids:
            # Special tokens have a word id that is None. We set the label to -100 so they are automatically
            # ignored in the loss function.
            if word_idx is None:
                label_ids.append(-100)
            # We set the label for the first token of each word.
            elif word_idx != previous_word_idx:
                try:
                    label_ids.append(self.tag2id[label_list[word_idx]])
                except:
                    count += 1
                    label_ids.append(-100)
            # For the other tokens in a word, we set the label to either the current label or -100, depending on
            # the label_all_tokens flag.
            else:
                try:
                    label_ids.append(self.tag2id[label_list[word_idx]] if self.label_all_tokens else -100)
                except:
                    count += 1
                    label_ids.append(-100)
            previous_word_idx = word_idx

        # print('Num error tags: {}'.format(count))
        tokenized_inputs["labels"] = torch.LongTensor(label_ids)
        tokenized_inputs['input_ids'] = torch.LongTensor(tokenized_inputs['input_ids'])
        tokenized_inputs['attention_mask'] = torch.LongTensor(tokenized_inputs['attention_mask'])
        return tokenized_inputs


class NERDataModule(LightningDataModule):

    def __init__(self,
                 model_name_or_path: str,
                 dataset_version: str,
                 tags_list: List[str],
                 label_all_tokens: bool = False,
                 max_seq_length: int = 128,
                 train_batch_size: int = 32,
                 eval_batch_size: int = 32,
                 test_size: float = 0.1,
                 val_size: float = 0.2,
                 **kwargs):
        super().__init__()

        self.val_data = None
        self.train_data = None
        self.dataset_df = None
        self.test_data = None
        self.save_hyperparameters(ignore=['model_name_or_path', 'tags_list', 'test_size', 'val_size'])

        self.model_name_or_path = model_name_or_path
        self.dataset_version = dataset_version
        self.tags_list = tags_list
        self.num_labels = len(tags_list)
        self.label_all_tokens = label_all_tokens

        self.val_size = val_size
        self.test_size = test_size

        self.max_seq_length = max_seq_length
        self.train_batch_size = train_batch_size
        self.eval_batch_size = eval_batch_size

    def prepare_data(self) -> None:
        pass

    @staticmethod
    def prepare_dataset(
            merge_sentence: Optional[int],
            val_size: float,
            test_size: float,
            dataset_path: str,
            output_dir: str,
            data_format: str,
            random_state: int = 43
    ):

        dataset_df = None
        if data_format == 'doccano':
            ner_dataset = NERDataSet(jsonl_file=dataset_path)
            dataset_df = ner_dataset.dataset_df
        elif data_format == 'csv':
            dataset_df = pd.read_csv(dataset_path)
            dataset_df['conll_label'] = dataset_df['conll_label'].map(lambda x: ast.literal_eval(x))

        if merge_sentence is not None:
            new_dataset = []
            for start_idx in range(0, len(dataset_df), merge_sentence)[:-1]:
                main_source = dataset_df.iloc[start_idx:start_idx+merge_sentence]['source'].value_counts(
                    dropna=False).index[0]

                new_conll_label = []
                for conll_label in list(dataset_df.iloc[start_idx:start_idx+merge_sentence]['conll_label'].values):
                    new_conll_label.extend(conll_label)

                new_dataset.append([main_source, new_conll_label])

            dataset_df = pd.DataFrame(data=new_dataset, columns=['source', 'conll_label'])

        dataset_df['source'].fillna('other', inplace=True)

        df_train, df_rest = train_test_split(dataset_df,
                                             shuffle=True,
                                             random_state=random_state,
                                             stratify=dataset_df[['source']],
                                             train_size=1 - val_size - test_size)

        df_val, df_test = train_test_split(df_rest,
                                           shuffle=True,
                                           random_state=43,
                                           stratify=df_rest[['source']],
                                           train_size=val_size / (val_size + test_size))

        # Write to file
        NERDataModule.write_to_file(df_data=df_train, output_dir=output_dir, filename='train_data_old.txt')
        NERDataModule.write_to_file(df_data=df_val, output_dir=output_dir, filename='val_data_old.txt')
        NERDataModule.write_to_file(df_data=df_test, output_dir=output_dir, filename='test_data_old.txt')

    @staticmethod
    def write_to_file(df_data, output_dir, filename):
        if not os.path.isdir(output_dir):
            os.mkdir(output_dir)

        with open(os.path.join(output_dir, filename), 'w') as file:
            for i in range(len(df_data)):
                item = df_data.iloc[i]
                conll_label = item['conll_label']

                file.write('\n'.join(conll_label))
                file.write('\n\n')

    def setup(self, stage: Optional[str] = None):

        self.train_data = CustomDataset(
            dataset_path=os.path.join('dataset', self.dataset_version, 'train_data.txt'),
            model_name_or_path=self.model_name_or_path,
            tags_list=self.tags_list,
            max_seq_length=self.max_seq_length,
            label_all_tokens=self.label_all_tokens)

        self.val_data = CustomDataset(
            dataset_path=os.path.join('dataset', self.dataset_version, 'val_data.txt'),
            model_name_or_path=self.model_name_or_path,
            tags_list=self.tags_list,
            max_seq_length=self.max_seq_length,
            label_all_tokens=self.label_all_tokens)

        self.test_data = CustomDataset(
            dataset_path=os.path.join('dataset', self.dataset_version, 'test_data.txt'),
            model_name_or_path=self.model_name_or_path,
            tags_list=self.tags_list,
            max_seq_length=self.max_seq_length,
            label_all_tokens=self.label_all_tokens)

    def train_dataloader(self):
        return DataLoader(self.train_data, batch_size=self.train_batch_size)

    def val_dataloader(self):
        return DataLoader(self.val_data, batch_size=self.eval_batch_size)

    def test_dataloader(self):
        return DataLoader(self.test_data, batch_size=self.eval_batch_size)

    def predict_dataloader(self):
        pass


if __name__ == '__main__':
    from dataset import NERDataModule

    # seed_everything(43)

    tags_list = ["B-ADDRESS", "I-ADDRESS",
                 "B-SKILL", "I-SKILL",
                 "B-EMAIL", "I-EMAIL",
                 "B-PERSON", "I-PERSON",
                 "B-PHONENUMBER", "I-PHONENUMBER",
                 "B-QUANTITY", "I-QUANTITY",
                 "B-PERSONTYPE", "I-PERSONTYPE",
                 "B-ORGANIZATION", "I-ORGANIZATION",
                 "B-PRODUCT", "I-PRODUCT",
                 "B-IP", 'I-IP',
                 "B-LOCATION", "I-LOCATION",
                 "O",
                 "B-DATETIME", "I-DATETIME",
                 "B-EVENT", "I-EVENT",
                 "B-URL", "I-URL"]

    # mlflow.pytorch.autolog(log_every_n_epoch=1)

    dm = NERDataModule(model_name_or_path='xlm-roberta-base',
                       dataset_version='version6_dont_merge',
                       tags_list=tags_list,
                       max_seq_length=128,
                       train_batch_size=32,
                       eval_batch_size=32)
    dm.prepare_dataset(
        merge_sentence=None,
        val_size=0.2,
        test_size=0.1,
        dataset_path='origin_dataset/all_data_v6_16t03.csv',
        output_dir='dataset/version6_dont_merge',
        data_format='csv',
        random_state=41
    )
