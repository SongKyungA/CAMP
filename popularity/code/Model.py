import torch
import torch.nn as nn
import Config
import torch.nn.functional as F

torch.manual_seed(2023)
torch.cuda.manual_seed(2023)

class ModulePopHistory(nn.Module):
    def __init__(self, config: Config.Config):
        super(ModulePopHistory, self).__init__()
        self.config = config
        self.fc_output_pop_history = nn.Linear(config.embed_size, 1)

    def ema(self, pop_history, valid_pop_len):
        alpha = self.config.alpha
        ema_all = []
        for i in range(len(pop_history)):
            ema_temp = []
            line = pop_history[i]
            for j in range(int(valid_pop_len[i].item())):
                if j == 0:
                    ema_temp.append(line[j])
                else:
                    ema_temp.append(alpha * line[j] + (1 - alpha) * ema_temp[j - 1])
            ema_all.append(ema_temp[-1])
        ema_all = torch.tensor(ema_all).to(self.config.device)
        return ema_all

    def forward(self, pop_history, valid_pop_len):
        history_average = self.ema(pop_history, valid_pop_len)
        return history_average.unsqueeze(-1)


class ModuleTime(nn.Module):
    def __init__(self, config: Config.Config):
        super(ModuleTime, self).__init__()
        self.config = config
        self.fc_item_pop_value = nn.Linear(config.embed_size*4, 1)
        self.relu = nn.ReLU()

    def forward(self, item_embed, time_release_embed, time_embed):
        temporal_dis = time_release_embed - time_embed
        item_temp_joint_embed = torch.cat((temporal_dis, item_embed, time_embed, time_release_embed), 1)
        joint_item_temp_value = self.relu(self.fc_item_pop_value(item_temp_joint_embed))
        return joint_item_temp_value


class ModuleSideInfo(nn.Module):
    def __init__(self, config: Config.Config):
        super(ModuleSideInfo, self).__init__()
        self.config = config
        if self.config.is_douban:
            self.fc_output = nn.Linear(3*config.embed_size, 1)
        else:
            self.fc_output = nn.Linear(2*config.embed_size, 1)
        self.relu = nn.ReLU()

    def forward(self, genre_embed, director_embed=None, actor_embed=None):
        genre_embed = genre_embed.mean(dim=1)
        if director_embed is not None and actor_embed is not None:
            # 이 조건문을 통해 director_embed와 actor_embed가 제공되었는지 확인
            actor_embed = actor_embed.mean(dim=1)
            embed_sideinfo = torch.cat((genre_embed, director_embed, actor_embed), 1)
        else:
            embed_sideinfo = torch.cat((genre_embed, director_embed), 1)
        output = self.relu(self.fc_output(embed_sideinfo))
        return output


class PopPredict(nn.Module):
    def __init__(self, is_training, config: Config.Config):
        super(PopPredict, self).__init__()

        self.config = config
        self.is_training = is_training
        num_genre = self.config.num_side_info[0]
        num_director = self.config.num_side_info[1] if self.config.is_douban else 1
        num_actor = self.config.num_side_info[2] if self.config.is_douban else 1

        self.embed_size = config.embed_size

        # Embedding layers
        self.embed_item = nn.Embedding(config.num_item, self.embed_size)
        self.embed_time = nn.Embedding(config.max_time + 1, self.embed_size)
        self.embed_genre = nn.Embedding(num_genre, self.embed_size, padding_idx=0)
        self.embed_director = nn.Embedding(num_director, self.embed_size, padding_idx=0)
        self.embed_actor = nn.Embedding(num_actor, self.embed_size, padding_idx=0)

        # Modules
        self.module_pop_history = ModulePopHistory(config=config)
        self.module_sideinfo = ModuleSideInfo(config=config)
        self.module_time = ModuleTime(config=config)

        # Attention mechanism
        self.attention_weights = nn.Parameter(torch.ones(3, 1) / 3)  # Adjusted for 3 modules
        self._init_weights()

    def _init_weights(self):
        for m in self.modules():
            if isinstance(m, nn.Linear):
                nn.init.kaiming_normal_(m.weight)
                if m.bias is not None:
                    nn.init.constant_(m.bias, 0)

    def forward(self, item, time_release, item_genre, item_director, item_actor, time, pop_history, pop_gt, valid_pop_len):
        item_embed = self.embed_item(item)
        time_release_embed = self.embed_time(time_release)
        genre_embed = self.embed_genre(item_genre)
        time_embed = self.embed_time(time)
        
        director_embed = self.embed_director(item_director) if self.config.is_douban else torch.zeros_like(genre_embed)
        actor_embed = self.embed_actor(item_actor) if self.config.is_douban else torch.zeros_like(genre_embed)

        # Module outputs
        pop_history_output = self.module_pop_history(pop_history, valid_pop_len)
        time_output = self.module_time(item_embed, time_release_embed, time_embed)
        
        if self.config.is_douban:
            sideinfo_output = self.module_sideinfo(genre_embed, director_embed, actor_embed)
        else:
            sideinfo_output = self.module_sideinfo(genre_embed)

        # Concatenate module outputs without the periodic module
        pred_all = torch.cat((pop_history_output, time_output, sideinfo_output), 1)

        # Apply attention weights (adjusted for 3 modules)
        normalized_weights = F.softmax(self.attention_weights, dim=0)
        output = torch.mm(pred_all, normalized_weights).squeeze()

        if not self.is_training:
            print('Attention weights:', normalized_weights.data.cpu().numpy())

        return pop_history_output, time_output, sideinfo_output, output
