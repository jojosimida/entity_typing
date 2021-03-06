import torch
from torch import nn, optim

import time
import numpy as np
from tqdm import tqdm

from evaluation import evaluate_data, decode_ent, decode_rel


# change the weight of the relation loss function 

# # =======hyper-parameter-set=========
# MAX_LEN = 100
# BATCH_SIZE = 32

# EMBEDDING_DIM = 64
# HIDDEN_DIM1 = 30  #30
# HIDDEN_DIM2 =  16 #16
# LABEL_EMBED_DIM = 8 #8

# ATTN_OUT = 8 #8

# n_iter = 20
# # =======hyper-parameter-set=========

class JointERE(nn.Module):
    def __init__(self, vocab_size, embedding_dim, hidden_dim1, hidden_dim2, \
                 label_embed_dim, attn_output, schema):
        '''
        JointERE
            Joint Entity and Relation mention Extraction on Traditional Chinese text
        Input:
            vocab_size:
                The number of all the indexed vocabularies or the total token embeddings
            embedding_dim:
                The dimension of each token embedding
            hidden_dim1:
                The dimension of the hidden state of the bidirection LSTM for input sentences
            hidden_dim2:
                The dimension of the top hidden LSTM layer for aggregation of sentence features
                and entity embeddings
            label_embed_dim:
                The dimension of embedding of each entity tag
            attn_output:
                The column dimension of the value matrix in the pointer network
            schema:
                An instance of data_util.Schema with definition of entities and relations
        '''
        
        
        super(JointERE, self).__init__()
        self.embedding_dim = embedding_dim                   #E
        self.hidden_dim1 = hidden_dim1                       #h1
        self.hidden_dim2 = hidden_dim2                       #h2
        self.label_embed_dim = label_embed_dim               #LE
        self.attn_output = attn_output
        self.vocab_size = vocab_size                         #vs
        self.schema = schema
        self.ent_size = len(schema.ent2ix)                   #es
        self.rel_size = len(schema.rel2ix)                   #rs       
   
        
        self.bn = nn.BatchNorm1d(hidden_dim1, momentum=0.5, affine=False)
        
        
        self.word_embeds = nn.Embedding(vocab_size, embedding_dim)
        nn.init.normal_(self.word_embeds.weight.data)
        
#         self.bilstm = nn.LSTM(embedding_dim, hidden_dim1 // 2,
#                             num_layers=2, bidirectional=True, batch_first=True, dropout=0.2)        
        self.bilstm = nn.GRU(embedding_dim, hidden_dim1 // 2,
                            num_layers=2, bidirectional=True, batch_first=True, dropout=0.2)
    
        for param in self.bilstm.parameters():
            if len(param.size()) >= 2:
                nn.init.orthogonal_(param.data)
            else:
                nn.init.normal_(param.data)
        

        self.top_hidden = nn.LSTMCell(hidden_dim1+label_embed_dim, hidden_dim2)    
        nn.init.orthogonal_(self.top_hidden.weight_ih.data)
        nn.init.orthogonal_(self.top_hidden.weight_hh.data)
        

        # Maps the output of the LSTM into tag space.
        self.hidden2tag = nn.Linear(hidden_dim2, self.ent_size)
        self.init_linear(self.hidden2tag)
        
        self.softmax = nn.LogSoftmax(dim=1)
        self.label_embed = nn.Linear(self.ent_size, self.label_embed_dim, bias=False)
        nn.init.orthogonal_(self.label_embed.weight.data)
        
        self.attn = Attn(hidden_dim2 + label_embed_dim, attn_output, self.rel_size)
        

        
    def init_linear(self, m):
        nn.init.xavier_normal_(m.weight.data)
        nn.init.normal_(m.bias.data)
        
    def one_hot(self, ent_choice):
        y_onehot = ent_choice.new_zeros(ent_choice.size(0), self.ent_size, dtype=torch.float)
        return y_onehot.scatter_(-1, ent_choice.unsqueeze(-1), 1)   
    
        
    def forward(self, sentence, batch_ent=None):
        '''Assume I/O resides on the same device, and so does this module'''
        
        batch_size, max_len = sentence.size()
        entity_tensor = torch.zeros(batch_size, max_len, self.ent_size, device=sentence.device)  #B*ML*es
#         rel_tensor = entity_tensor.new_full((batch_size, max_len, max_len, self.rel_size), np.NINF)  #B*ML*ML*rs
        rel_tensor = torch.zeros(batch_size, max_len, max_len, self.rel_size, device=sentence.device)  #B*ML*ML*rs

        embeds = self.word_embeds(sentence)                     #B*ML*E
        
        bilstm_out, hidden1 = self.bilstm(embeds)
        # bilstm_out -> B*ML*h1
        # hidden1 -> ( 4*B*(h1/2), 4*B*(h1/2) )
        
        # bn
        bilstm_out = self.bn(bilstm_out.transpose(1, 2)).transpose(1, 2)
        
        encoder_sequence_l = [] 
        
        h_next = bilstm_out.new_empty(batch_size, self.hidden_dim2).normal_()
        c_next = torch.randn_like(h_next)
        label = h_next.new_zeros(batch_size, self.label_embed_dim)

        for length in range(max_len):
            now_token = bilstm_out[:,length,:]
            now_token = torch.squeeze(now_token, 1)
            combine_x = torch.cat((now_token, label), 1)   #B*(h1+LE)
            
            
            h_next, c_next = self.top_hidden(combine_x, (h_next, c_next))   #B*h2
            to_tags = self.hidden2tag(h_next)                               #B*es
            ent_output = self.softmax(to_tags)                              #B*es
            label = self.label_embed(self.one_hot(ent_output.argmax(-1)))   #B*LE
            
            
            # pass the gold entity embedding to the next time step, if available
            if batch_ent is not None:
                label = self.label_embed(self.one_hot(batch_ent[:, length]))
            
            
                    
            # relation layer
            encoder_sequence_l.append(torch.cat((h_next,label),1))  
            encoder_sequence = torch.stack(encoder_sequence_l).transpose(0, 1)     #B*len*(h2+LE)         

            # Calculate attention weights 
            attn_weights = self.attn(encoder_sequence)
      

            entity_tensor[:,length,:] = ent_output
            
            # rel_tensor[:,length, head~now ,:]
            rel_tensor[:,length,:length+1,:] = attn_weights
 

        
        return entity_tensor, rel_tensor
        
        
    def entity_loss(self):
        pad_idx = self.schema.ent2ix[self.schema.PAD_TAG]
        return EntityNLLLoss(ignore_index=pad_idx)
    
    def relation_loss(self):
        pad_idx = self.schema.rel2ix[self.schema.REL_PAD]
        return RelationNLLLoss(ignore_index=pad_idx)
    
    
    def fit(self, loader, dev_loader, optimizer=None, n_iter=50, stable_iter=10, true_ent=False,
            save_model=None):
        
        '''
        Fit JointERE and select paramteres based on validation score
        Input:
            loader:
                The instance of data_util.BIOLoader containing training data
            dev_loader:
                The instance of data_util.BIOLoader containing development/validation data
            optimizer: optional
                An specified optimizer from torch.optim for training model.
                By default Adam(lr=0.01, weight_decay=1e-4, amsgrad=True) would be used.
            n_iter: optional
                The total traing epochs to fit and search the best model.
                The default value is 50 epochs.
            stable_iter: optional
                The epoch after which the model begins to evaluate and select model.
                The default value is 10 epochs.
            save_model: optional
                The path to store model parameters during model selection
                If unspecified, the path defaults to 'checkpoints/relation_extraction_best.{time}.pkl'
        '''
            
        
        criterion_tag = self.entity_loss()
        criterion_rel = self.relation_loss()
        optimizer = optimizer or optim.Adam(self.parameters(), lr=0.01, weight_decay=1e-4, amsgrad=True)
        
        for epoch in tqdm(range(n_iter)):
            for batch_x, batch_ent, batch_rel, batch_index in loader:
                self.train()
                optimizer.zero_grad()
                
                if true_ent:
                    ent_output, rel_output = self.forward(batch_x, batch_ent)
                
                else:
                    ent_output, rel_output = self.forward(batch_x)
                
                batch_loss_ent = criterion_tag(ent_output, batch_ent)
                batch_loss_rel = criterion_rel(rel_output, batch_rel)  
                batch_loss = batch_loss_ent + batch_loss_rel
                
                

                batch_loss.backward()
                
                # `clip_grad_norm` helps prevent the exploding gradient problem in RNNs / LSTMs.
                torch.nn.utils.clip_grad_norm_(self.parameters(), 0.25)
                
                optimizer.step()
                
            for batch_x, batch_ent, batch_rel, batch_index in dev_loader:
                self.eval()
                ent_output, rel_output = self.forward(batch_x, batch_ent)
                
                batch_loss_ent_dev = criterion_tag(ent_output, batch_ent)
                batch_loss_rel_dev = criterion_rel(rel_output, batch_rel)  
            
            
            print("epoch: %d | ent loss %.4f | rel loss %.4f | total loss %.4f" \
          % (epoch+1, batch_loss_ent, batch_loss_rel, batch_loss))
            print("      %s  | val ent loss %.4f | val rel loss %.4f"
          % (" "*len(str(epoch+1)), batch_loss_ent_dev, batch_loss_rel_dev))
            
        return self
    
    
    def predict(self, X):
        
        '''
        Predict entity and relation mentions
        
        Input:
            X: a batch*max_len array/tensor of indexed sentences
            threshold: a float of relation detection threshold
        Output:
            entities: a list of entity mention sets in the batch.
                A entity mention set is a list of tuples with start/end
                offset and the entity type id from schema.
                [(ent_start, ent_end, ent_type=eid_in_schema)]
            relations: a list of relation mention sets in the batch.
                A relation mention set is a list of tuples with
                subject/object argument of entity and relation type
                id from schema.
                [(e1, e2, rid)]
        '''
            
        entities, relations = [], []
        self.eval()
        ent_outputs, rel_outputs = self.forward(X)
        
        for e, r in zip(ent_outputs.cpu(), rel_outputs.cpu()):
            ent_mentions, _ = decode_ent(e, self.schema)
            rel_mentions = decode_rel(ent_mentions, r, self.schema)
            entities.append(ent_mentions)
            relations.append(rel_mentions)
        
        return entities, relations

    
    def score(self, loader, isTrueEnt=False, silent=False, rel_detail=False, analyze=False):
        '''
        Compute Precision/Recall/F-1 score of dataset in BIOLoader X
        Input:
            loader: a BIOLoader containing a dataset of sentences.
            isTrueEnt: optional. Boolean to give the ground truth entity to evaluate
            silent: optional. Boolean to suppress detailed decoding
            rel_detail: optional. Boolean to show the each relation's precision, recall and F1 score.
            analyze: optional. Boolean to draw the distribution of distance of entity pair in predict.
        Output:
            e_score: P/R/F-1 score of entity prediction
            er_score: P/R/F-1 score of entity and relation prediction
        '''
        
        if rel_detail==True:
            e_score, er_score, all_er_score = evaluate_data(self, loader, self.schema, isTrueEnt, silent, rel_detail, analyze)
            return e_score, er_score, all_er_score
        
        else:
            e_score, er_score = evaluate_data(self, loader, self.schema, isTrueEnt, silent, rel_detail, analyze)
            return e_score, er_score
    
    
    
    
    
    def save_model(self, name='relation_extraction_1_new.pth'):
        torch.save(self.state_dict(), name)
                
        
        
    
    
    
class Attn(nn.Module):
    def __init__(self, attn_input, attn_output, rel_size):
        super(Attn, self).__init__()
        
        self.attn_input = attn_input
        self.attn_output = attn_output
        self.rel_size = rel_size
        
        self.w1 = nn.Linear(self.attn_input, self.attn_output, bias=False)
        nn.init.xavier_normal_(self.w1.weight.data)
        self.w2 = nn.Linear(self.attn_input, self.attn_output, bias=False)
        nn.init.xavier_normal_(self.w2.weight.data)  
        self.tanh = nn.Tanh()   
        self.v = nn.Linear(self.attn_output, self.rel_size, bias=False)
        nn.init.xavier_normal_(self.v.weight.data)
        
        self.softmax = nn.LogSoftmax(dim=-1)
        
        
    def forward(self, encoder_outputs):
#         encoder_outputs = self.seq_attn(encoder_outputs)
        
        decoder = encoder_outputs[:,-1,:].unsqueeze(1)                       #B*1*(ts+LE) 
        encoder_score = self.w1(encoder_outputs)                             #B*now len*ATTN_OUT
        decoder_score = self.w2(decoder)                                     #B*1*ATTN_OUT
        energy = self.tanh(encoder_score+decoder_score)                      #B*now len*ATTN_OUT   
        
        energy = self.v(energy)                                              #B*now len*rel_size
        
        p = self.softmax(energy)                        
        
        return p                                                             #B*now len*rel_size
    
    
# class Sequence_wise_Attn(nn.Module):
#     def __init__(self, attn_dropout=0.1):
#         super().__init__()        
        
        
#         self.dropout = nn.Dropout(attn_dropout)
#         self.softmax = nn.LogSoftmax(dim=-1)


#     def forward(self, encoder_outputs):
#         temperature = encoder_outputs.size(2)
#         temperature = np.power(temperature, 0.5)
#         residual = encoder_outputs        
        
#         query = encoder_outputs[:,-1,:].unsqueeze(1)                       #B*1*(d_model) 
#         key = encoder_outputs                                              #B*now len*(d_model) 
#         value = encoder_outputs

#         attn = torch.bmm(query, key.transpose(1, 2))
#         attn = attn / temperature
        
#         attn = self.softmax(attn)                                          #B*1*now len
#         attn = self.dropout(attn)

#         encoder_outputs = attn.transpose(1,2)*encoder_outputs
#         encoder_outputs = encoder_outputs + residual                       #B*now len*(d_model) 
        
#         return encoder_outputs    
    
    
    
    
class EntityNLLLoss(nn.NLLLoss):
    
    def __init__(self, **kwargs):
        kwargs['reduction'] = 'none'
        super().__init__(**kwargs)
        
    def forward(self, outputs, labels):
        loss = super(EntityNLLLoss, self).forward(outputs.transpose(1, 2).unsqueeze(2),
                                                  labels.unsqueeze(1))
        return mean_sentence_loss(loss)


class RelationNLLLoss(nn.NLLLoss):    
    def __init__(self, **kwargs):
        kwargs['reduction'] = 'none'
        super().__init__(**kwargs)

        
    def forward(self, outputs, labels):
        loss = super(RelationNLLLoss, self).forward(outputs.permute(0,-1,1,2),labels)
        maxlen = labels.size(1)
        mask_weight = torch.ones(maxlen, maxlen, device=labels.device)
        for i, _ in enumerate(mask_weight):
            mask_weight[i,:i+1] *= 50

        return mean_sentence_loss(loss*mask_weight)

    
    
def mean_sentence_loss(loss):
    num_tokens = loss.norm(0, -1)
    return loss.sum(dim=-1).div(num_tokens).mean()

