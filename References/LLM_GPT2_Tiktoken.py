import pandas as pd
import torch
import torch.nn as nn
from torch.nn import functional as F
import os

device = 'cuda' if torch.cuda.is_available() else 'cpu'

batch_size = 2 #1
block_size = 128 #8 #512
max_iters = 10000 #2 #50000
learning_rate = 1e-4
eval_iters = 100 #1 #1000
n_embd = 256 #4 #768
n_head = 64 #2 #12
n_layer = 128 #2 #12
dropout = 0.5 #0.2

#qa_data = pd.read_excel(r'D:\ML_Projects\LLM-From-Scratch-For-ChatBots-GPT2\Data\Huggingface\Customer_Support_Hugg_Preprocessed.xlsx')
tokenizer = tiktoken.get_encoding("cl100k_base")

stock_data = pd.read_csv(r'D:\ML_Projects\Stock-Market-Prediction\NIFTY 50_minute_data.csv')
stock_data['open'] = stock_data['open'].apply(lambda x: round(x))
vocab_size = stock_data['open'].nunique()

class Head(nn.Module):
    """ one head of self-attention """

    def __init__(self, head_size):
        super().__init__()
        self.key = nn.Linear(n_embd, head_size, bias=False)
        self.query = nn.Linear(n_embd, head_size, bias=False)
        self.value = nn.Linear(n_embd, head_size, bias=False)
        self.register_buffer('tril', torch.tril(torch.ones(block_size, block_size)))
        self.dropout = nn.Dropout(dropout)

    def forward(self, x):
        # input of size (batch, time-step, channels)
        # output of size (batch, time-step, head size)
        B,T,C = x.shape
        k = self.key(x)   # (B,T,hs)
        q = self.query(x) # (B,T,hs)
        # compute attention scores ("affinities")
        wei = q @ k.transpose(-2,-1) * k.shape[-1]**-0.5 # (B, T, hs) @ (B, hs, T) -> (B, T, T)
        wei = wei.masked_fill(self.tril[:T, :T] == 0, float('-inf')) # (B, T, T)
        wei = F.softmax(wei, dim=-1) # (B, T, T)
        wei = self.dropout(wei)
        # perform the weighted aggregation of the values
        v = self.value(x) # (B,T,hs)
        out = wei @ v # (B, T, T) @ (B, T, hs) -> (B, T, hs)
        return out

class MultiHeadAttention(nn.Module):
    """ multiple heads of self-attention in parallel """

    def __init__(self, num_heads, head_size):
        super().__init__()
        self.heads = nn.ModuleList([Head(head_size) for _ in range(num_heads)])
        self.proj = nn.Linear(head_size * num_heads, n_embd)
        self.dropout = nn.Dropout(dropout)

    def forward(self, x):
        out = torch.cat([h(x) for h in self.heads], dim=-1) # (B, T, F) -> (B, T, [h1, h1, h1, h1, h2, h2, h2, h2, h3, h3, h3, h3])
        out = self.dropout(self.proj(out))
        return out
    

class FeedFoward(nn.Module):
    """ a simple linear layer followed by a non-linearity """

    def __init__(self, n_embd):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(n_embd, 4 * n_embd),
            nn.ReLU(),
            nn.Linear(4 * n_embd, n_embd),
            nn.Dropout(dropout),
        )

    def forward(self, x):
        return self.net(x)
    
class Block(nn.Module):
    """ Transformer block: communication followed by computation """

    def __init__(self, n_embd, n_head):
        # n_embd: embedding dimension, n_head: the number of heads we'd like
        super().__init__()
        head_size = n_embd // n_head
        self.sa = MultiHeadAttention(n_head, head_size)
        self.ffwd = FeedFoward(n_embd)
        self.ln1 = nn.LayerNorm(n_embd)
        self.ln2 = nn.LayerNorm(n_embd)

    def forward(self, x):
        y = self.sa(x)
        x = self.ln1(x + y)
        y = self.ffwd(x)
        x = self.ln2(x + y)
        return x
    
class GPTLanguageModel(nn.Module):
    def __init__(self, vocab_size):
        super().__init__()
        self.token_embedding_table = nn.Embedding(vocab_size, n_embd)
        self.position_embedding_table = nn.Embedding(block_size, n_embd)
        self.blocks = nn.Sequential(*[Block(n_embd, n_head=n_head) for _ in range(n_layer)])
        self.ln_f = nn.LayerNorm(n_embd) # final layer norm
        self.lm_head = nn.Linear(n_embd, vocab_size)
        self.apply(self._init_weights)

    def _init_weights(self, module):
        if isinstance(module, nn.Linear):
            torch.nn.init.normal_(module.weight, mean=0.0, std=0.02)
            if module.bias is not None:
                torch.nn.init.zeros_(module.bias)
        elif isinstance(module, nn.Embedding):
            torch.nn.init.normal_(module.weight, mean=0.0, std=0.02)

    def forward(self, index, targets=None):
        B, T = index.shape
        # idx and targets are both (B,T) tensor of integers
        tok_emb = self.token_embedding_table(index) # (B,T,C)
        pos_emb = self.position_embedding_table(torch.arange(T, device=device)) # (T,C)
        x = tok_emb + pos_emb # (B,T,C)
        x = self.blocks(x) # (B,T,C)
        x = self.ln_f(x) # (B,T,C)
        logits = self.lm_head(x) # (B,T,vocab_size)
        
        if targets is None:
            loss = None
        else:
            B, T, C = logits.shape
            logits = logits.view(B*T, C)
            targets = targets.view(B*T)
            loss = F.cross_entropy(logits, targets)
        
        return logits, loss
    
    def generate(self, index, max_new_tokens):
        # index is (B, T) array of indices in the current context
        for _ in range(max_new_tokens):
            index_cond = index[:, -block_size:]  # crop idx to the last block_size tokens
            logits, loss = self.forward(index_cond)  # get the predictions
            logits = logits[:, -1, :] # becomes (B, C) # focus only on the last time step
            probs = F.softmax(logits, dim=-1) # (B, C) # apply softmax to get probabilities
            index_next = torch.multinomial(probs, num_samples=1) # (B, 1) # sample from the distribution
            index = torch.cat((index, index_next), dim=1) # (B, T+1) # append sampled index to the running sequence
        return index

def get_batch(qa_tokenized, split):
    X = []
    Y = []
    for i in range(batch_size):
        rand_row = torch.randint(0, len(qa_tokenized), (1,))
        token_len = block_size + 1
        tokens = qa_tokenized[rand_row]
        padded_tokens = tokens + [0] * (token_len - len(tokens)) if len(tokens) < token_len else tokens[:token_len]
        X.append(padded_tokens[:-1])
        Y.append(padded_tokens[1:])
    x_batch = torch.tensor(X, dtype=torch.long)
    y_batch = torch.tensor(Y, dtype=torch.long)
    return x_batch.to(device), y_batch.to(device)

qa_tokenized = process_data(qa_data, tokenizer)

@torch.no_grad()
def estimate_loss():
    out = {}
    model.eval()
    for split in ['train', 'val']:
        losses = torch.zeros(eval_iters)
        for k in range(eval_iters):
            X, Y = get_batch(qa_tokenized, split)
            logits, loss = model(X, Y)
            losses[k] = loss.item()
        out[split] = losses.mean()
    model.train()
    return out

if __name__ == "__main__":
    #Start over training on the pre-trained model with the same data
    #saved_model_path = r'D:\ML_Projects\LLM-From-Scratch-For-ChatBots-GPT2\Models\model-22_loss-0.159.pth'
    model = GPTLanguageModel(vocab_size)
    #model = torch.load(saved_model_path, map_location=device)
    model.to(device)

    optimizer = torch.optim.AdamW(model.parameters(), lr=learning_rate)

    for iter in range(max_iters):
        if iter % eval_iters == 0:
            losses = estimate_loss()
            print(f"step: {iter}, train loss: {losses['train']:.3f}, val loss: {losses['val']:.3f}")
            torch.save(model, os.path.join(r'D:\ML_Projects\LLM-From-Scratch-For-ChatBots-GPT2\Models\model_checkpoints', f'model_temp_{iter}_loss-{losses["val"]:.3f}.pth'))

        # sample a batch of data
        xb, yb = get_batch(qa_tokenized, 'train')
        # evaluate the loss
        logits, loss = model.forward(xb, yb)
        optimizer.zero_grad(set_to_none=True)
        loss.backward()
        optimizer.step()
    print(loss.item())

    torch.save(model, os.path.join(r'D:\ML_Projects\LLM-From-Scratch-For-ChatBots-GPT2\Models', f'model-25_loss-{loss.item():.3f}.pth'))
