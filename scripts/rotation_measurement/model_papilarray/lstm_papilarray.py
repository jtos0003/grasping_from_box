from sqlite3 import adapt
import torch
from torch import random
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import Dataset, DataLoader, random_split
import pandas as pd
import numpy as np
import os
import matplotlib.pyplot as plt
import copy
import time
import wandb
import argparse
import sys
import yaml
from pathlib import Path
from arg_set import parse_arguments
from enum import Enum
import random

class SampleType(Enum):
    FRONT = 0
    CENTER = 1
    RANDOM = 2


class TactileDataset(Dataset):
    def __init__(self, path, normalize=False, mode=None, seq_length=None, label_scale = 1, sample_type = SampleType.RANDOM):
        self.data_path = path if mode is None else path+mode+'/'
        self.initial_path = os.getcwd()
        os.chdir(self.data_path)
        self.datapoints = os.listdir('.')
        os.chdir(self.initial_path)
        self.seq_length = seq_length
        self.label_scale = label_scale
        self.normalize = normalize
        if normalize:
            self.max_values = np.load("max_values.npy")
            self.min_values = np.load("min_values.npy")
            self.keep_index = np.load("keep_index.npy")

            self.min_angle = -119.604454
            self.max_angle = 167.61925


        self.sample_type = sample_type
    
    def __len__(self):
        return len(self.datapoints)
    
    def scale(self, x, out_range=(-1, 1), domain=(-1, 1)):
        
        if type(domain[0]) == np.ndarray:
            domain_tmp0 = np.tile(domain[0], (x.shape[0], 1))
            domain_tmp1 = np.tile(domain[1], (x.shape[0], 1))

            domain = (domain_tmp0, domain_tmp1) 

        y = (x - (domain[1] + domain[0]) / 2) / (domain[1] - domain[0])
        return y * (out_range[1] - out_range[0]) + (out_range[1] + out_range[0]) / 2

    def getItem(self, i):
        df = pd.read_csv(self.data_path + self.datapoints[i])
        return df.values

    def __getitem__(self, i):
        df = pd.read_csv(self.data_path + self.datapoints[i])
        if self.normalize:
            true_values = np.take(df.values, self.keep_index.squeeze(), axis=1)
        else:
            true_values = df.values

        angle = None
        df_tensor = None        
        if self.seq_length is None:

            if self.normalize:

                normalized = self.scale(true_values[:, :-2], domain=(self.min_values[:-2], self.max_values[:-2])) 
                angle = self.scale(true_values[:, -2], domain=(self.min_values[-2], self.max_values[-2]))
                # print(self.min_values[-2], self.max_values[-2], true_values[:, -2],  angle)

                df_tensor = torch.Tensor(normalized).float()

            else:
                # only normalize angle
                df_tensor = torch.Tensor(true_values[:, :-2]).float()
                angle =  true_values[:,-2] / self.label_scale
        else:
            df_tensor = torch.Tensor(true_values[:, :-2]).float()
            angle =  true_values[:,-2] / self.label_scale
        
        # -2 because the time step is ignored
        return df_tensor, angle
    
    def collate_fn(self, batch):

        # 0 is the data, 1 is GT
        min_length = min(map(lambda x : x[0].shape[0], batch))

        # https://www.geeksforgeeks.org/python-k-middle-elements/
        if self.seq_length is None:
            K = min_length
            # print("HERE", K)
        elif self.seq_length > min_length:
            print(self.seq_length,' ',min_length)
            ValueError('Seq length is too long!')
        else:
            K = self.seq_length

        torch_array = torch.zeros((len(batch), K, 142))
        gt_array = torch.zeros((len(batch), K))        
        # print(torch_array.shape)
        # input()   

        for (index, (data, gt)) in enumerate(batch):

            # computing strt, and end index 
            strt_idx = 0 #(len(data) // 2) - (K // 2)
            end_idx = (len(data) // 2) + (K // 2)

            if self.sample_type == SampleType.CENTER:
                strt_idx = (len(data) // 2) - (K // 2)
                # print(K, strt_idx, len(data))
            elif self.sample_type == SampleType.FRONT:
                strt_idx = 0
            elif self.sample_type == SampleType.RANDOM:
                max_start_value = len(data) - K
                # print()
                strt_idx = random.randrange(0, max_start_value+1, 1)
                # print(strt_idx)
            else:
                ValueError('Invalid sample type')
            # print(data.shape, gt.shape)
            # print(data_cropped.shape, gt_cropped.shape)
            # print("cropped size", data[strt_idx: end_idx + 1, :].shape)
            # slicing extracting middle elements
            data_cropped = data[strt_idx: strt_idx + K, :]
            gt_cropped = gt[strt_idx: strt_idx + K]
            # print(data_cropped.shape, gt_cropped.shape)
            # input()            
            torch_array[index, :, :] = data_cropped
            gt_array[index, :] = torch.tensor(gt_cropped - gt_cropped[0])
            # print((gt_cropped - gt_cropped[0]))
            # print(gt_cropped)
            # input()

        return torch_array, gt_array
        

class RegressionLSTM(nn.Module):
    def __init__(self, device, num_features, hidden_size, num_layers, dropout):
        super().__init__()
        self.num_features = num_features
        self.hidden_size = hidden_size
        self.num_layers = num_layers
        self.device = device

        # self.start_linear1 = nn.Linear(in_features=self.num_features, out_features=200)
        # self.start_linear2 = nn.Linear(in_features=200, out_features=200)
        # self.start_linear3 = nn.Linear(in_features=200, out_features=self.num_features)
        # self.sig = nn.Sigmoid()

        self.lstm = nn.LSTM(
            input_size = self.num_features,
            hidden_size = self.hidden_size,
            batch_first = True,
            num_layers = self.num_layers,
            dropout = dropout
        )

        # self.out_lin1 = nn.Linear(self.hidden_size, self.hidden_size)
        # self.out_lin2 = nn.Linear(self.hidden_size, self.hidden_size)
        
        self.output_linear_final = nn.Linear(in_features=self.hidden_size, out_features=1)

    def init_model_state(self, batch_size):
        h0 = torch.zeros(self.num_layers, batch_size, self.hidden_size).requires_grad_().to(self.device)
        c0 = torch.zeros(self.num_layers, batch_size, self.hidden_size).requires_grad_().to(self.device)
        return (h0, c0)
        
    def forward(self, x):
        # print("x.shape", x.shape)
        # input()
        
        batch_size = x.shape[0]

        h0, c0 = self.init_model_state(batch_size)
        # x = self.start_linear1(x)
        # x = self.start_linear2(x)

        # print(x.shape, h0.shape, c0.shape)
        # x = self.start_linear3(self.sig(self.start_linear2(self.sig(self.start_linear1(x)))))
        
        #out is size (batch, seq, feat*layers) for batch_first=True
        out, hidden = self.lstm(x, (h0, c0))

        # out = out[:, -1, :]

        # print(batch_size, h0.shape, c0.shape, out.shape, hn.shape, cn.shape)
        # print(out.contiguous().view(-1, out.size(2)))
        # print(out.shape)
        #Fold the batch and seq dimensions together, so each sequence will be basically like a batch element
        # out = self.linear(out.contiguous().view(-1, out.size(2)))
        # print(out.shape)

        # out = self.sig(self.out_lin1(out))
        # out = self.sig(self.out_lin2(out))
        out = self.output_linear_final(out)

        # print(out.shape)

        return out
    

    
def train(device, loader, model, loss_func, optim, l1loss):
    model.train()
    loss_count = 0
    abs_error_count = 0
    
    for i, (features, label) in enumerate(loader):
        out = model(features.to(device))
        # print(out.shape, label.shape)
        loss = loss_func(out.squeeze(), label.to(device).squeeze())
        
        loss_count += loss.item()
        
        optim.zero_grad()
        loss.backward()
        optim.step()
        
        l1error = l1loss(out.squeeze(), label.to(device).squeeze())
        abs_error_count += l1error.item()        

    loss_count /= i+1
    abs_error_count /= i+1
    
    return loss_count, abs_error_count

def test(device, loader, model, loss_func, optim, l1loss):
    model.eval()

    loss_count = 0
    abs_error_count = 0

    with torch.no_grad():
        for i, (features, label) in enumerate(loader):
            out = model(features.to(device))
            loss = loss_func(out.squeeze(), label.to(device).squeeze())
            l1error = l1loss(out.squeeze(), label.to(device).squeeze())
            
            loss_count += loss.item()
            abs_error_count += l1error.item()
            
        loss_count /= i+1
        abs_error_count /= i+1
    
    return loss_count, abs_error_count


def main():
    sample_type = SampleType.RANDOM
    seq_length = None

    # Parse args
    args = parse_arguments()
    
    # Check if sweeping or using default config
    if len(sys.argv) == 1:
        config_file = args.config
        with open(config_file, 'r') as f:
            cfg_input = yaml.safe_load(f) 
    elif len(sys.argv) > 1:
        del args.config
        cfg_input = args
    else:
        raise ValueError("Weird arg error")
    

    run = wandb.init(project="Ruler-papilarray", 
                     entity="deep-tactile-rotatation-estimation", 
                     config=cfg_input,
                     mode="disabled"
    )
    # run = wandb.init(project="SRP", config=cfg_input)
    config = wandb.config
    print(config)
    
    #Set device to GPU_indx if GPU is avaliable
    GPU_indx = 0
    device = torch.device(GPU_indx if torch.cuda.is_available() else 'cpu')
    
    # Create dataset/dataloaders
    data = TactileDataset(config["data_path"], label_scale = config["label_scale"], sample_type=sample_type, seq_length=seq_length, normalize=config["normalize"])
    train_data_length = round(len(data)*config["train_frac"])
    test_data_length = len(data) - train_data_length
    train_data, test_data = random_split(data, [train_data_length, test_data_length], generator=torch.Generator().manual_seed(42))

    train_loader = DataLoader(train_data, batch_size = config["train_batch_size"], shuffle=True, collate_fn=data.collate_fn)
    test_loader = DataLoader(test_data, batch_size = config["test_batch_size"], shuffle=True, collate_fn=data.collate_fn)
    
    # Create model
    model = RegressionLSTM(device, config["num_features"], config["hidden_size"], config["num_layers"], config["dropout"])
    if config["resume_from_checkpoint"]:
        model.load_state_dict(torch.load(config["model_path"]))
    model = model.to(device)
    
    # Define loss and optimization functions
    loss_func = nn.MSELoss()
    l1loss = nn.L1Loss()
    
    optim = torch.optim.Adam(model.parameters(), lr=config["learning_rate"], weight_decay=config["weight_decay"])
    
    
    if config["test_only"]:
        loss_train, abs_error_train = test(device, train_loader, model, loss_func, optim, l1loss)
        loss_test, abs_error_test = test(device, test_loader, model, loss_func, optim, l1loss)
        wandb.log({
                "Loss/train":loss_train,
                "Loss/test":loss_test,
                "abs_error/train":abs_error_train*config["label_scale"],
                "abs_error/test":abs_error_test*config["label_scale"],
            })
        print('Train error: %f, Train loss: %f, Test error: %f, Test loss: %f'%(abs_error_train*config["label_scale"], loss_train, abs_error_test*config["label_scale"], loss_test))
    else:
        # Run training
        best_model = copy.deepcopy(model)
        lowest_error = 1e5
        old_time = time.time()
        for i in range(config["num_epochs"]):
            loss_train, abs_error_train = train(device, train_loader, model, loss_func, optim, l1loss)
            loss_test, abs_error_test = test(device, test_loader, model, loss_func, optim, l1loss)

            wandb.log({
                "Loss/train":loss_train,
                "Loss/test":loss_test,
                "abs_error/train":abs_error_train*config["label_scale"],
                "abs_error/test":abs_error_test*config["label_scale"],
            })

            new_time = time.time()
            print("Epoch: %i, Test error: %f, Test loss: %f, Time taken: %.2f sec/epoch" % (i, abs_error_test*config["label_scale"], loss_test, new_time-old_time) )
            old_time = copy.deepcopy(new_time)
            if abs_error_test < lowest_error:
                best_model_dict = copy.deepcopy(model.state_dict())
                torch.save(best_model_dict, config["model_path"])
                lowest_error = abs_error_test
                print("new best!")
        
        print("Lowest error was: %f"%(lowest_error*config["label_scale"]))
        
        artifact = wandb.Artifact('model', type='model')
        artifact.add_file(config["model_path"]) 
        run.log_artifact(artifact)
        # run.join()  
    
    #Load best model
    best_model = RegressionLSTM(device, config["num_features"], config["hidden_size"], config["num_layers"], config["dropout"])
    best_model.load_state_dict(torch.load(config["model_path"]))
    best_model = best_model.to(device)
    
    #Refresh loaders                        
    train_loader = DataLoader(train_data, batch_size = 1, shuffle=True, collate_fn=data.collate_fn)
    test_loader = DataLoader(test_data, batch_size = 1, shuffle=True, collate_fn=data.collate_fn)
        
    #plot that shows labels/out of some sequences
    fig, axs = plt.subplots(10, 10)
    for ax in axs.flat:
        features, label = next(iter(train_loader))
        count = 0
        while(max(label.squeeze())-min(label.squeeze()) < 5/config["label_scale"]) and count < len(train_data):
            features, label = next(iter(train_loader))
            count += 1

        out = best_model(features.to(device))
        out = out.squeeze()
        label = label.squeeze()
        x_range = [*range(len(out))]
        ax.plot(x_range, label.detach().to('cpu')*config["label_scale"], label = 'Ground truth')
        ax.plot(x_range, out.detach().to('cpu')*config["label_scale"], label = 'Prediction')
    fig.suptitle("Train examples")
    wandb.log({'Examples/Train': fig})
    # plt.savefig(plot_path + 'examples_train.png')
    # plt.show()

    fig, axs = plt.subplots(10, 10)
    for ax in axs.flat:
        features, label = next(iter(test_loader))
        count = 0
        while(max(label.squeeze())-min(label.squeeze()) < 5/config["label_scale"]) and count < len(test_data):
            features, label = next(iter(test_loader))
            count += 1
        out = best_model(features.to(device))
        out = out.squeeze()
        label = label.squeeze()
        x_range = [*range(len(out))]
        ax.plot(x_range, label.detach().to('cpu')*config["label_scale"], label = 'Ground truth')
        ax.plot(x_range, out.detach().to('cpu')*config["label_scale"], label = 'Prediction')
    
    fig.suptitle("Test examples")
    wandb.log({'Examples/Test': fig})
    # plt.savefig(plot_path + 'examples_train.png')
    # plt.show()
    
    print("Training complete!")
    

if __name__=="__main__":
    main()