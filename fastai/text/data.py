"NLP data loading pipeline. Supports csv, folders, and preprocessed data."
from ..torch_core import *
from .transform import *
from ..basic_data import *
from ..data_block import *
from ..layers import *
from ..callback import Callback

__all__ = ['LanguageModelPreLoader', 'SortSampler', 'SortishSampler', 'TextList', 'pad_collate', 'TextDataBunch',
           'TextLMDataBunch', 'TextClasDataBunch', 'Text', 'open_text', 'TokenizeProcessor', 'NumericalizeProcessor',
           'OpenFileProcessor', 'LMLabelList']

TextMtd = IntEnum('TextMtd', 'DF TOK IDS')
text_extensions = {'.txt'}

class LanguageModelPreLoader(Callback):
    "Transforms the tokens in `dataset` to a stream of contiguous batches for language modelling."
    
    class CircularIndex():
        "Handles shuffle, direction of indexing, wraps around to head tail in the ragged array as needed"
        def __init__(self, length:int, forward:bool): self.idx, self.forward = np.arange(length), forward
        def __getitem__(self, i): 
            return self.idx[ i%len(self.idx) if self.forward else len(self.idx)-1-i%len(self.idx)]
        def __len__(self) -> int: return len(self.idx)
        def shuffle(self): np.random.shuffle(self.idx)

    def __init__(self, dataset:LabelList, lengths:Collection[int]=None, bs:int=32, bptt:int=70, backwards:bool=False, 
                 shuffle:bool=False):
        self.dataset,self.bs,self.bptt,self.shuffle,self.backwards,self.lengths = dataset,bs,bptt,shuffle,backwards,lengths
        self.totalToks,self.ite_len,self.idx = int(0),None,None

    def __len__(self): 
        if self.ite_len is None:
            if self.lengths is None: self.lengths = np.array([len(item) for item in self.dataset.x.items])
            self.totalToks = self.lengths.sum()
            self.ite_len   = self.bs*int( math.ceil( self.totalToks/(self.bptt*self.bs) )) if self.item is None else 1
        return self.ite_len

    def __getattr__(self,k:str)->Any: return getattr(self.dataset, k)
   
    def allocate_buffers(self):
        "Create the ragged array that will be filled when we ask for items."
        if self.ite_len is None: len(self)
        self.idx   = LanguageModelPreLoader.CircularIndex(len(self.dataset.x.items), not self.backwards)
        self.batch = np.zeros((self.bs, self.bptt+1), dtype=np.int64)
        self.batch_x, self.batch_y = self.batch[:,0:self.bptt], self.batch[:,1:self.bptt+1] 
        #ro: index of the text we're at inside our datasets for the various batches
        self.ro    = np.zeros(self.bs, dtype=np.int64)
        #ri: index of the token we're at inside our current text for the various batches
        self.ri    = np.zeros(self.bs, dtype=np.int)

    def on_epoch_begin(self, **kwargs):
        if self.idx is None: self.allocate_buffers()
        elif self.shuffle:   self.idx.shuffle()
        self.idx.forward = not self.backwards 

        step = self.totalToks / self.bs
        ln_rag, countTokens, i_rag = 0, 0, -1
        for i in range(0,self.bs):
            #Compute the initial values for ro and ri 
            while ln_rag + countTokens <= int(step * i):
                countTokens += ln_rag
                i_rag       += 1
                ln_rag       = self.lengths[self.idx[i_rag]]
            self.ro[i] = i_rag
            self.ri[i] = ( ln_rag - int(step * i - countTokens) ) if self.backwards else int(step * i - countTokens)
        
    #Training dl gets on_epoch_begin called, val_dl, on_epoch_end
    def on_epoch_end(self, **kwargs): self.on_epoch_begin()

    def __getitem__(self, k:int):
        j = k % self.bs
        if j==0:
            if self.item is not None: return self.dataset[0]
            if self.idx is None: self.on_epoch_begin()
        self.ro[j],self.ri[j] = self.fill_row(not self.backwards, self.dataset.x.items, self.idx, self.batch[j], 
                                              self.ro[j], self.ri[j], overlap=1, lengths=self.lengths)
        return self.batch_x[j], self.batch_y[j]

    def fill_row(self, forward, items, idx, row, ro, ri, overlap,lengths):
        "Fill the row with tokens from the ragged array. --OBS-- overlap != 1 has not been implemented"
        ibuf = n = 0 
        ro  -= 1
        while ibuf < row.size:  
            ro   += 1 
            ix    = idx[ro]
            rag   = items[ix]
            if forward:
                ri = 0 if ibuf else ri
                n  = min(lengths[ix] - ri, row.size - ibuf)
                row[ibuf:ibuf+n] = rag[ri:ri+n]
            else:    
                ri = lengths[ix] if ibuf else ri
                n  = min(ri, row.size - ibuf) 
                row[ibuf:ibuf+n] = rag[ri-n:ri][::-1]
            ibuf += n
        return ro, ri + ((n-overlap) if forward else -(n-overlap))

class SortSampler(Sampler):
    "Go through the text data by order of length."

    def __init__(self, data_source:NPArrayList, key:KeyFunc): self.data_source,self.key = data_source,key
    def __len__(self) -> int: return len(self.data_source)
    def __iter__(self):
        return iter(sorted(range_of(self.data_source), key=self.key, reverse=True))

class SortishSampler(Sampler):
    "Go through the text data by order of length with a bit of randomness."

    def __init__(self, data_source:NPArrayList, key:KeyFunc, bs:int):
        self.data_source,self.key,self.bs = data_source,key,bs

    def __len__(self) -> int: return len(self.data_source)

    def __iter__(self):
        idxs = np.random.permutation(len(self.data_source))
        sz = self.bs*50
        ck_idx = [idxs[i:i+sz] for i in range(0, len(idxs), sz)]
        sort_idx = np.concatenate([sorted(s, key=self.key, reverse=True) for s in ck_idx])
        sz = self.bs
        ck_idx = [sort_idx[i:i+sz] for i in range(0, len(sort_idx), sz)]
        max_ck = np.argmax([self.key(ck[0]) for ck in ck_idx])  # find the chunk with the largest key,
        ck_idx[0],ck_idx[max_ck] = ck_idx[max_ck],ck_idx[0]     # then make sure it goes first.
        sort_idx = np.concatenate(np.random.permutation(ck_idx[1:])) if len(ck_idx) > 1 else np.array([],dtype=np.int)
        sort_idx = np.concatenate((ck_idx[0], sort_idx))
        return iter(sort_idx)

def pad_collate(samples:BatchSamples, pad_idx:int=1, pad_first:bool=True, backwards:bool=False) -> Tuple[LongTensor, LongTensor]:
    "Function that collect samples and adds padding. Flips token order if needed"
    samples = to_data(samples)
    max_len = max([len(s[0]) for s in samples])
    res = torch.zeros(len(samples), max_len).long() + pad_idx
    if backwards: pad_first = not pad_first
    for i,s in enumerate(samples):
        if pad_first: res[i,-len(s[0]):] = LongTensor(s[0])
        else:         res[i,:len(s[0]):] = LongTensor(s[0])
    if backwards:
        res = res.flip(1)
    return res, tensor(np.array([s[1] for s in samples]))

def _get_processor(tokenizer:Tokenizer=None, vocab:Vocab=None, chunksize:int=10000, max_vocab:int=60000,
                   min_freq:int=2, mark_fields:bool=False):
    return [TokenizeProcessor(tokenizer=tokenizer, chunksize=chunksize, mark_fields=mark_fields),
            NumericalizeProcessor(vocab=vocab, max_vocab=max_vocab, min_freq=min_freq)]

class TextDataBunch(DataBunch):
    "General class to get a `DataBunch` for NLP. Subclassed by `TextLMDataBunch` and `TextClasDataBunch`."

    def save(self, cache_name:PathOrStr='tmp'):
        "Save the `DataBunch` in `self.path/cache_name` folder."
        os.makedirs(self.path/cache_name, exist_ok=True)
        cache_path = self.path/cache_name
        pickle.dump(self.train_ds.vocab.itos, open(cache_path/'itos.pkl','wb'))
        np.save(cache_path/f'train_ids.npy', self.train_ds.x.items)
        np.save(cache_path/f'train_lbl.npy', self.train_ds.y.items)
        np.save(cache_path/f'valid_ids.npy', self.valid_ds.x.items)
        np.save(cache_path/f'valid_lbl.npy', self.valid_ds.y.items)
        if self.test_dl is not None: np.save(cache_path/f'test_ids.npy', self.test_ds.x.items)
        if hasattr(self.train_ds, 'classes'): save_texts(cache_path/'classes.txt', self.train_ds.classes)

    @classmethod
    def from_ids(cls, path:PathOrStr, vocab:Vocab, train_ids:Collection[Collection[int]], valid_ids:Collection[Collection[int]],
                 test_ids:Collection[Collection[int]]=None, train_lbls:Collection[Union[int,float]]=None,
                 valid_lbls:Collection[Union[int,float]]=None, classes:Collection[Any]=None,
                 processor:PreProcessor=None, **kwargs) -> DataBunch:
        "Create a `TextDataBunch` from ids, labels and a `vocab`. `kwargs` are passed to the dataloader creation."
        src = ItemLists(path, TextList(train_ids, vocab, path=path, processor=[]),
                        TextList(valid_ids, vocab, path=path, processor=[]))
        src = src.label_for_lm() if cls==TextLMDataBunch else src.label_from_lists(train_lbls, valid_lbls, classes=classes, processor=[])
        if not is1d(train_lbls): src.train.y.one_hot,src.valid.y.one_hot = True,True
        if test_ids is not None: src.add_test(TextList(test_ids, vocab, path=path), label=train_lbls[0])
        src.valid.x.processor = ifnone(processor, [TokenizeProcessor(), NumericalizeProcessor(vocab=vocab)])
        return src.databunch(**kwargs)

    @classmethod
    def load(cls, path:PathOrStr, cache_name:PathOrStr='tmp', processor:PreProcessor=None, **kwargs):
        "Load a `TextDataBunch` from `path/cache_name`. `kwargs` are passed to the dataloader creation."
        cache_path = Path(path)/cache_name
        vocab = Vocab(pickle.load(open(cache_path/'itos.pkl','rb')))
        train_ids,train_lbls = np.load(cache_path/f'train_ids.npy'), np.load(cache_path/f'train_lbl.npy')
        valid_ids,valid_lbls = np.load(cache_path/f'valid_ids.npy'), np.load(cache_path/f'valid_lbl.npy')
        test_ids = np.load(cache_path/f'test_ids.npy') if os.path.isfile(cache_path/f'test_ids.npy') else None
        classes = loadtxt_str(cache_path/'classes.txt') if os.path.isfile(cache_path/'classes.txt') else None
        return cls.from_ids(path, vocab, train_ids, valid_ids, test_ids, train_lbls, valid_lbls, classes, processor, **kwargs)

    @classmethod#TODO: test
    def from_tokens(cls, path:PathOrStr, trn_tok:Collection[Collection[str]], trn_lbls:Collection[Union[int,float]],
                 val_tok:Collection[Collection[str]], val_lbls:Collection[Union[int,float]], vocab:Vocab=None,
                 tst_tok:Collection[Collection[str]]=None, classes:Collection[Any]=None, max_vocab:int=60000, min_freq:int=3,
                 **kwargs) -> DataBunch:
        "Create a `TextDataBunch` from tokens and labels. `kwargs` are passed to the dataloader creation."
        processor = NumericalizeProcessor(vocab=vocab, max_vocab=max_vocab, min_freq=min_freq)
        src = ItemLists(path, TextList(trn_tok, path=path, processor=processor),
                        TextList(val_tok, path=path, processor=processor))
        src = src.label_for_lm() if cls==TextLMDataBunch else src.label_from_lists(trn_lbls, val_lbls, classes=classes)
        if tst_tok is not None: src.add_test(TextList(tst_tok, path=path))
        return src.databunch(**kwargs)

    @classmethod
    def from_df(cls, path:PathOrStr, train_df:DataFrame, valid_df:DataFrame, test_df:Optional[DataFrame]=None,
                tokenizer:Tokenizer=None, vocab:Vocab=None, classes:Collection[str]=None, text_cols:IntsOrStrs=1,
                label_cols:IntsOrStrs=0, label_delim:str=None, chunksize:int=10000, max_vocab:int=60000,
                min_freq:int=2, mark_fields:bool=False, **kwargs) -> DataBunch:
        "Create a `TextDataBunch` from DataFrames. `kwargs` are passed to the dataloader creation."
        processor = _get_processor(tokenizer=tokenizer, vocab=vocab, chunksize=chunksize, max_vocab=max_vocab,
                                   min_freq=min_freq, mark_fields=mark_fields)
        if classes is None and is_listy(label_cols) and len(label_cols) > 1: classes = label_cols
        src = ItemLists(path, TextList.from_df(train_df, path, cols=text_cols, processor=processor),
                        TextList.from_df(valid_df, path, cols=text_cols, processor=processor))
        if cls==TextLMDataBunch: src = src.label_for_lm() 
        else: src = src.label_from_df(cols=label_cols, classes=classes, label_delim=label_delim)
        if test_df is not None: src.add_test(TextList.from_df(test_df, path, cols=text_cols))
        return src.databunch(**kwargs)

    @classmethod
    def from_csv(cls, path:PathOrStr, csv_name, valid_pct:float=0.2, test:Optional[str]=None,
                 tokenizer:Tokenizer=None, vocab:Vocab=None, classes:Collection[str]=None, header = 'infer', text_cols:IntsOrStrs=1,
                 label_cols:IntsOrStrs=0, label_delim:str=None, chunksize:int=10000, max_vocab:int=60000,
                min_freq:int=2, mark_fields:bool=False, **kwargs) -> DataBunch:
        "Create a `TextDataBunch` from texts in csv files. `kwargs` are passed to the dataloader creation."
        df = pd.read_csv(Path(path)/csv_name, header=header)
        df = df.iloc[np.random.permutation(len(df))]
        cut = int(valid_pct * len(df)) + 1
        train_df, valid_df = df[cut:], df[:cut]
        test_df = None if test is None else pd.read_csv(Path(path)/test, header=header)
        return cls.from_df(path, train_df, valid_df, test_df, tokenizer=tokenizer, vocab=vocab, classes=classes, text_cols=text_cols,
                           label_cols=label_cols, label_delim=label_delim, chunksize=chunksize, max_vocab=max_vocab,
                          min_freq=min_freq, mark_fields=mark_fields, **kwargs)

    @classmethod
    def from_folder(cls, path:PathOrStr, train:str='train', valid:str='valid', test:Optional[str]=None,
                    classes:Collection[Any]=None, tokenizer:Tokenizer=None, vocab:Vocab=None, chunksize:int=10000, max_vocab:int=60000,
                    min_freq:int=2, mark_fields:bool=False, **kwargs):
        "Create a `TextDataBunch` from text files in folders."
        path = Path(path).absolute()
        processor = _get_processor(tokenizer=tokenizer, vocab=vocab, chunksize=chunksize, max_vocab=max_vocab,
                                   min_freq=min_freq, mark_fields=mark_fields)
        src = (TextList.from_folder(path, processor=processor)
                       .split_by_folder(train=train, valid=valid))
        src = src.label_for_lm() if cls==TextLMDataBunch else src.label_from_folder(classes=classes)
        if test is not None: src.add_test_folder(path/test)
        return src.databunch(**kwargs)

class TextLMDataBunch(TextDataBunch):
    "Create a `TextDataBunch` suitable for training a language model."
    @classmethod
    def create(cls, train_ds, valid_ds, test_ds=None, path:PathOrStr='.', no_check:bool=False, bs=64, num_workers:int=0,
               device:torch.device=None, collate_fn:Callable=data_collate, dl_tfms:Optional[Collection[Callable]]=None, 
               bptt:int=70, backwards:bool=False) -> DataBunch:
        "Create a `TextDataBunch` in `path` from the `datasets` for language modelling."
        datasets = cls._init_ds(train_ds, valid_ds, test_ds)
        datasets = [LanguageModelPreLoader(ds, shuffle=(i==0), bs=bs, bptt=bptt, backwards=backwards) 
                    for i,ds in enumerate(datasets)]
        val_bs = bs
        dls = [DataLoader(d, b, shuffle=False) for d,b in zip(datasets, (bs,val_bs,val_bs,val_bs)) if d is not None]
        return cls(*dls, path=path, device=device, dl_tfms=dl_tfms, collate_fn=collate_fn, no_check=no_check)
    
class TextClasDataBunch(TextDataBunch):
    "Create a `TextDataBunch` suitable for training an RNN classifier."
    @classmethod
    def create(cls, train_ds, valid_ds, test_ds=None, path:PathOrStr='.', bs=64, pad_idx=1, pad_first=True,
               device:torch.device=None, no_check:bool=False, backwards:bool=False, **kwargs) -> DataBunch:
        "Function that transform the `datasets` in a `DataBunch` for classification."
        datasets = cls._init_ds(train_ds, valid_ds, test_ds)
        collate_fn = partial(pad_collate, pad_idx=pad_idx, pad_first=pad_first, backwards=backwards)
        train_sampler = SortishSampler(datasets[0].x, key=lambda t: len(datasets[0][t][0].data), bs=bs//2)
        train_dl = DataLoader(datasets[0], batch_size=bs//2, sampler=train_sampler, drop_last=True, **kwargs)
        dataloaders = [train_dl]
        for ds in datasets[1:]:
            lengths = [len(t) for t in ds.x.items]
            sampler = SortSampler(ds.x, key=lengths.__getitem__)
            dataloaders.append(DataLoader(ds, batch_size=bs, sampler=sampler, **kwargs))
        return cls(*dataloaders, path=path, device=device, collate_fn=collate_fn, no_check=no_check)

def open_text(fn:PathOrStr, enc='utf-8'):
    "Read the text in `fn`."
    with open(fn,'r', encoding = enc) as f: return ''.join(f.readlines())

class Text(ItemBase):
    "Basic item for <code>text</code> data in numericalized `ids`."
    def __init__(self, ids, text): self.data,self.text = np.array(ids, dtype=np.int64),text
    def __str__(self):  return str(self.text)

class TokenizeProcessor(PreProcessor):
    "`PreProcessor` that tokenizes the texts in `ds`."
    def __init__(self, ds:ItemList=None, tokenizer:Tokenizer=None, chunksize:int=10000, mark_fields:bool=False):
        self.tokenizer,self.chunksize,self.mark_fields = ifnone(tokenizer, Tokenizer()),chunksize,mark_fields

    def process_one(self, item):  return self.tokenizer._process_all_1([item])[0]
    def process(self, ds):
        ds.items = _join_texts(ds.items, self.mark_fields)
        tokens = []
        for i in progress_bar(range(0,len(ds),self.chunksize), leave=False):
            tokens += self.tokenizer.process_all(ds.items[i:i+self.chunksize])
        ds.items = tokens

class NumericalizeProcessor(PreProcessor):
    "`PreProcessor` that numericalizes the tokens in `ds`."
    def __init__(self, ds:ItemList=None, vocab:Vocab=None, max_vocab:int=60000, min_freq:int=3):
        vocab = ifnone(vocab, ds.vocab if ds is not None else None)
        self.vocab,self.max_vocab,self.min_freq = vocab,max_vocab,min_freq

    def process_one(self,item): return np.array(self.vocab.numericalize(item), dtype=np.int64)
    def process(self, ds):
        if self.vocab is None: self.vocab = Vocab.create(ds.items, self.max_vocab, self.min_freq)
        ds.vocab = self.vocab
        super().process(ds)

class OpenFileProcessor(PreProcessor):
    "`PreProcessor` that opens the filenames and read the texts."
    def process_one(self,item):
        return open_text(item) if isinstance(item, Path) else item

class TextList(ItemList):
    "Basic `ItemList` for text data."
    _bunch = TextClasDataBunch
    _processor = [TokenizeProcessor, NumericalizeProcessor]
    _is_lm = False

    def __init__(self, items:Iterator, vocab:Vocab=None, pad_idx:int=1, **kwargs):
        super().__init__(items, **kwargs)
        self.vocab,self.pad_idx = vocab,pad_idx
        self.copy_new += ['vocab', 'pad_idx']

    def get(self, i):
        o = super().get(i)
        return Text(o, self.vocab.textify(o))

    def label_for_lm(self, **kwargs):
        "A special labelling method for language models."
        self.__class__ = LMTextList
        return self.label_const(0, label_cls=LMLabelList)

    def reconstruct(self, t:Tensor):
        idx = (t != self.pad_idx).nonzero().min()
        return Text(t[idx:], self.vocab.textify(t[idx:]))

    @classmethod
    def from_folder(cls, path:PathOrStr='.', extensions:Collection[str]=text_extensions, vocab:Vocab=None,
                    processor:PreProcessor=None, **kwargs)->'TextList':
        "Get the list of files in `path` that have a text suffix. `recurse` determines if we search subfolders."
        processor = ifnone(processor, [OpenFileProcessor(), TokenizeProcessor(), NumericalizeProcessor(vocab=vocab)])
        return super().from_folder(path=path, extensions=extensions, processor=processor, **kwargs)

    def show_xys(self, xs, ys, max_len:int=70)->None:
        "Show the `xs` (inputs) and `ys` (targets). `max_len` is the maximum number of tokens displayed."
        from IPython.display import display, HTML
        items = [['idx','text']] if self._is_lm else [['text','target']]
        for i, (x,y) in enumerate(zip(xs,ys)):
            txt_x = ' '.join(x.text.split(' ')[:max_len]) if max_len is not None else x.text
            items.append([str(i), str(txt_x)] if self._is_lm else [str(txt_x), str(y)])
        display(HTML(text2html_table(items, ([5,95] if self._is_lm else [90,10]))))

    def show_xyzs(self, xs, ys, zs, max_len:int=70):
        "Show `xs` (inputs), `ys` (targets) and `zs` (predictions). `max_len` is the maximum number of tokens displayed."
        from IPython.display import display, HTML
        items = [['text','target','prediction']]
        for i, (x,y,z) in enumerate(zip(xs,ys,zs)):
            txt_x = ' '.join(x.text.split(' ')[:max_len]) if max_len is not None else x.text
            items.append([str(txt_x), str(y), str(z)])
        display(HTML(text2html_table(items,  [85,7.5,7.5])))

class LMLabelList(EmptyLabelList):
    "Basic `ItemList` for dummy labels."
    def __init__(self, items:Iterator, **kwargs):
        super().__init__(items, **kwargs)
        self.loss_func = CrossEntropyFlat()

class LMTextList(TextList):
    "Special `TextList` for a language model."
    _bunch = TextLMDataBunch
    _is_lm = True

def _join_texts(texts:Collection[str], mark_fields:bool=False):
    if not isinstance(texts, np.ndarray): texts = np.array(texts)
    if is1d(texts): texts = texts[:,None]
    df = pd.DataFrame({i:texts[:,i] for i in range(texts.shape[1])})
    text_col = f'{BOS} {FLD} {1} ' + df[0].astype(str) if mark_fields else  f'{BOS} ' + df[0].astype(str)
    for i in range(1,len(df.columns)):
        text_col += (f' {FLD} {i+1} ' if mark_fields else ' ') + df[i].astype(str)   
    return text_col.values
