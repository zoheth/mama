from typing import Optional
from collections import deque
import copy

from pandas import DataFrame
from tqdm import tqdm

import sys
sys.path.append('dags')
from common.utils import DFHandler


class PretrainingTextHandler(DFHandler):
    def __init__(self):
        super().__init__()

    def handle(self, df: DataFrame) -> Optional[DataFrame]:
        col = self.columns_obj
        # 转换为str 防止数字数据
        df[col.text] = df[col.text].apply(str)
        data = []
        cur_customer = None
        last_timestamp = 1700000000
        for i, row in tqdm(df.iterrows(), total=df.shape[0], mininterval=5):
            text = row[col.text]
            if row[col.speaker] == 0:
                prefix = '客户：'
            else:
                prefix = '销售：'
            if row[col.timestamp] - last_timestamp > 3600 or row['mess_count'] + row['voice_count'] > 5:
                if row[col.customer_id] == cur_customer:
                    data[-1] = data[-1] + '<eop>'
            if row[col.customer_id] != cur_customer:
                if cur_customer is not None:
                    data.append('<eod>')
                cur_customer = row[col.customer_id]
            data.append(prefix + text)
            last_timestamp = row[col.timestamp]
        df = DataFrame(data, columns=['data'])

        return df

class PretrainingLongTextHandler(DFHandler):
    def __init__(self):
        super().__init__()

    def handle(self, df: DataFrame, df_s: DataFrame) -> Optional[DataFrame]:
        col = self.columns_obj
        ids=df_s.loc[df_s['num_c_msg']>2]['customer_id'].values
        df=df.loc[df['customer_id'].isin(ids)]
        # 转换为str 防止数字数据
        df[col.text] = df[col.text].apply(str)
        data = []
        cur_customer = None
        last_timestamp = 1700000000
        for i, row in tqdm(df.iterrows(), total=df.shape[0], mininterval=5):
            text = row[col.text]
            if row[col.speaker] == 0:
                prefix = '客户：'
            else:
                prefix = '销售：'
            if row[col.timestamp] - last_timestamp > 3600 or row['mess_count'] + row['voice_count'] > 5:
                if row[col.customer_id] == cur_customer:
                    data[-1] = data[-1] + '<eop>'
            if row[col.customer_id] != cur_customer:
                if cur_customer is not None:
                    data.append('<eod>')
                cur_customer = row[col.customer_id]
            data.append(prefix + text)
            last_timestamp = row[col.timestamp]
        df = DataFrame(data, columns=['data'])

        return df


class PretrainingDataHandler(DFHandler):
    def __init__(self):
        super().__init__()

    def handle(self, df: DataFrame) -> Optional[DataFrame]:
        from transformers import AutoTokenizer
        import tensorflow as tf
        def _int64_feature(values):
            return tf.train.Feature(int64_list=tf.train.Int64List(value=values))
        def get_feature(data):
            feature = {
                "input": _int64_feature(data['input_ids']),
                "is_masked": _int64_feature(data['attention_mask']),
                "target": _int64_feature(data['input_ids']),
                "seg_id": _int64_feature(data['token_type_ids'])}
            return feature
        
        tokenizer = AutoTokenizer.from_pretrained("hfl/chinese-xlnet-base")
        texts = df['data'].apply(str).values
        features = []
        mem1=[] # 记录将要处理的文本
        mem2=[] # 记录下一次要处理的文本 从客户端开始
        cur_len=0
        reply=0 # 销售回复数
        reply2=0
        for text in tqdm(texts, total=len(texts), mininterval=5):
            if text=='<eod>':
                if reply>0:
                    res=tokenizer(' '.join(mem1),max_length=256, truncation='longest_first', padding='max_length',add_special_tokens=False)
                    features.append(get_feature(res))
                mem1.clear()
                mem2.clear()
                cur_len=0
                reply=0
                reply2=0
                continue
            
            if text.startswith('客户'):
                mem2.clear()
                reply=0
            else:
                reply+=1
                reply2+=1

            mem1.append(text)
            mem2.append(text)

            cur_len+=len(tokenizer.encode(text,add_special_tokens=False))
            if cur_len>=256:
                res=tokenizer(' '.join(mem1),max_length=256, truncation='longest_first', padding='max_length',add_special_tokens=False)
                mem1=copy.deepcopy(mem2)
                reply=reply2
                reply2=0
                cur_len=len(tokenizer.encode(' '.join(mem1),add_special_tokens=False))
                mem2.clear()
                features.append(get_feature(res))
            
        record_writer = tf.compat.v1.python_io.TFRecordWriter('1.tfrecord')
        for feature in features:
            example = tf.train.Example(features=tf.train.Features(feature=feature))
            record_writer.write(example.SerializeToString())
        record_writer.close()

class BenchmarkHandler(DFHandler):
    def __init__(self, model_path):
        super().__init__()
        self.model_path = model_path

    def get_input(self, contexts, tokenizer):
        inputs = []
        length = 0
        for ctx in contexts:
            length += len(ctx)
        while length > 200:
            x = contexts.popleft()
            length -= len(x)
        for ctx in contexts:
            inputs.append(tokenizer.encode(ctx, add_special_tokens=False, return_tensors='tf'))
        prompt = "销售："
        inputs.append(tokenizer.encode(
            prompt, add_special_tokens=False, return_tensors='tf'))
        import tensorflow as tf
        inputs = tf.concat(inputs, 1)
        return inputs

    def handle(self, df: DataFrame) -> Optional[DataFrame]:
        from transformers import AutoTokenizer
        from transformers.models.xlnet.modeling_tf_xlnet import TFXLNetLMHeadModel
        model = TFXLNetLMHeadModel.from_pretrained(self.model_path)
        model.transformer.attn_type = 'bi'
        tokenizer = AutoTokenizer.from_pretrained("hfl/chinese-xlnet-base")
        pretraining_text_handler = PretrainingTextHandler()
        df = pretraining_text_handler.handle(df)
        contexts = deque()
        data = []
        # df=df.head(200)
        for i, row in tqdm(df.iterrows(), total=df.shape[0], mininterval=5):
            if row['data'] == '<eod>':
                contexts.clear()
                data.append('')
                continue
            contexts.append(row['data'])
            if row['data'][0] == '客' and len(contexts) > 0:
                inputs = self.get_input(contexts, tokenizer)
                leng = inputs.shape[-1]
                print(leng)
                outputs = model.generate(inputs,
                                         num_beams=20, 
                                         max_length=leng+20,
                                         do_sample=True, top_p=0.95,
                                         num_return_sequences=3,
                                         no_repeat_ngram_size=3,
                                         early_stopping=True)
                result = []
                for i, sample_output in enumerate(outputs):
                    generated = tokenizer.decode(
                        sample_output[leng:], skip_special_tokens=False)
                    result.append(f'{i}: {generated}')
                data.append('\n'.join(result))
                print(result[0])
            else:
                data.append('')
        df.insert(1, 'nlg', data)
        return df


class IdsHandler(DFHandler):
    def __init__(self):
        super().__init__()

    def handle(self, df: DataFrame) -> Optional[DataFrame]:
        from transformers import AutoTokenizer

        tokenizer = AutoTokenizer.from_pretrained("hfl/chinese-xlnet-base")
        df['data']=df['data'].astype(str)
        df['data']=df['data'].transform(lambda x: ' '.join(map(str,tokenizer.encode(x, add_special_tokens=False))))
        return df

if __name__ == '__main__':
    import pandas as pd
    bh = BenchmarkHandler('/home/yangkaixuan/datafile/airflow/mymodel3')
    # pdh=IdsHandler()
    df = pd.read_csv(
        '/home/yangkaixuan/datafile/airflow/nlg_preprocess/2021-12-22/premium.csv')
    df = bh.handle(df)
    # df=pd.read_csv('temp.csv')
    # df=df['data']
    df.to_csv('result3-1.csv',index=False)
