import logging
import sys 
sys.path.append('/kaggle/working/kbqa_system')
from work.TopicWordRecognization.utils import read, convert_example_to_feature, set_seed, SeqEntityScore
from work.TopicWordRecognization.model import ErnieNER
from functools import partial

import paddle
from paddlenlp.datasets import load_dataset
from paddlenlp.transformers import ErnieTokenizer, ErnieModel, LinearDecayWithWarmup
from paddlenlp.data import Pad, Tuple

from work.config import NERConfig

logger = logging.getLogger()


def train():
    train_ds = load_dataset(read, data_path=train_path, lazy=False)  # 文件->example
    dev_ds = load_dataset(read, data_path=dev_path, lazy=False)

    tokenizer = ErnieTokenizer.from_pretrained(model_name)
    trans_func = partial(convert_example_to_feature, tokenizer=tokenizer, label2id=label2id,
                         pad_default_tag="O", max_seq_len=max_seq_len)

    train_ds = train_ds.map(trans_func, lazy=False)  # example->feature
    dev_ds = dev_ds.map(trans_func, lazy=False)

    batchify_fn = lambda samples, fn=Tuple(
        Pad(axis=0, pad_val=tokenizer.pad_token_id),
        Pad(axis=0, pad_val=tokenizer.pad_token_type_id),
        Pad(axis=0, pad_val=label2id["O"], dtype='int64'),
    ): fn(samples)

    train_batch_sampler = paddle.io.BatchSampler(train_ds, batch_size=batch_size, shuffle=True)
    dev_batch_sampler = paddle.io.BatchSampler(dev_ds, batch_size=batch_size, shuffle=False)
    train_loader = paddle.io.DataLoader(dataset=train_ds, batch_sampler=train_batch_sampler, collate_fn=batchify_fn,
                                        return_list=True)
    dev_loader = paddle.io.DataLoader(dataset=dev_ds, batch_sampler=dev_batch_sampler, collate_fn=batchify_fn,
                                      return_list=True)

    ernie = ErnieModel.from_pretrained(model_name)
    model = ErnieNER(ernie, len(label2id), dropout=0.1)

    num_training_steps = len(train_loader) * num_epoch
    lr_scheduler = LinearDecayWithWarmup(learning_rate, num_training_steps, warmup_proportion)
    decay_params = [p.name for n, p in model.named_parameters() if not any(nd in n for nd in ["bias", "norm"])]
    grad_clip = paddle.nn.ClipGradByGlobalNorm(max_grad_norm)
    optimizer = paddle.optimizer.AdamW(learning_rate=lr_scheduler, parameters=model.parameters(),
                                       weight_decay=weight_decay, apply_decay_param_fun=lambda x: x in decay_params,
                                       grad_clip=grad_clip)

    loss_model = paddle.nn.CrossEntropyLoss()
    ner_metric = SeqEntityScore(id2label)

    global_step, ner_best_f1 = 0, 0.
    model.train()
    for epoch in range(1, num_epoch + 1):
        for batch_data in train_loader:
            input_ids, token_type_ids, labels = batch_data
            logits = model(input_ids, token_type_ids=token_type_ids)

            loss = loss_model(logits, labels)

            loss.backward()
            lr_scheduler.step()
            optimizer.step()
            optimizer.clear_grad()

            if global_step > 0 and global_step % log_step == 0:
                print(
                    f"epoch: {epoch} - global_step: {global_step}/{num_training_steps} - loss:{loss.numpy().item():.6f}")
            if global_step > 0 and global_step % eval_step == 0:

                ner_results = evaluate(model, dev_loader, ner_metric)
                ner_result = ner_results["Total"]
                model.train()
                ner_f1 = ner_result["F1"]
                if ner_f1 > ner_best_f1:
                    paddle.save(model.state_dict(), f"{save_path}/ernie_ner_best.pdparams")
                if ner_f1 > ner_best_f1:
                    print(f"\nner best F1 performence has been updated: {ner_best_f1:.5f} --> {ner_f1:.5f}")
                    ner_best_f1 = ner_f1
                print(
                    f'\nner evalution result: precision: {ner_result["Precision"]:.5f}, recall: {ner_result["Recall"]:.5f},  F1: {ner_result["F1"]:.5f}, current best {ner_best_f1:.5f}\n')

            global_step += 1


def evaluate(model, data_loader, metric):
    model.eval()
    metric.reset()
    for idx, batch_data in enumerate(data_loader):
        input_ids, token_type_ids, labels, = batch_data
        logits = model(input_ids, token_type_ids=token_type_ids)
        pred_labels = logits.argmax(axis=-1)
        metric.update(pred_paths=pred_labels, real_paths=labels)
    results = metric.get_result()

    return results

def load_ner_model(model_path):
    loaded_state_dict = paddle.load(model_path)
    ernie = ErnieModel.from_pretrained(model_name)
    model = ErnieNER(ernie, len(label2id), dropout=0.1)
    model.load_dict(loaded_state_dict)
    tokenizer = ErnieTokenizer.from_pretrained(model_name)
    model.eval()
    return model,tokenizer

def predict(model,tokenizer, input_text):
    # model_path = f"{save_path}/ernie_ner_best.pdparams"
    splited_input_text = list(input_text)
    features = tokenizer(splited_input_text, is_split_into_words=True, max_seq_len=max_seq_len, return_length=True)
    input_ids = paddle.to_tensor(features["input_ids"]).unsqueeze(0)
    token_type_ids = paddle.to_tensor(features["token_type_ids"]).unsqueeze(0)
    seq_len = features["seq_len"]

    ner_logits = model(input_ids, token_type_ids=token_type_ids)

    # parse ner labels
    ner_pred_labels = ner_logits.argmax(axis=-1).numpy()[0][1:(seq_len) - 1]
    ner_labels = []
    for idx in ner_pred_labels:
        ner_label = id2label[idx]
        if ner_label != "O":
            ner_label = list(id2label[idx])
            ner_label[1] = "-"
            ner_label = "".join(ner_label)
        ner_labels.append(ner_label)
    ner_entities = SeqEntityScore(label2id).get_entities_bio(ner_labels)

    ner_results = []
    for ner_entity in ner_entities:
        entity_name, start, end = ner_entity
        # print(f"{entity_name}: ", "".join(splited_input_text[start:end + 1]))
        ner_results.append("".join(splited_input_text[start:end + 1]))
    return ner_results


model_name = "ernie-1.0"
# model_name = "ERNIE3.0-Base"
max_seq_len = 512
batch_size = 32
label2id = {"O": 0, "B-LOC": 1, "I-LOC": 2}
id2label = {0: "O", 1: "B-LOC", 2: "I-LOC"}

num_epoch = 5
learning_rate = 2e-5
weight_decay = 0.01
warmup_proportion = 0.1
max_grad_norm = 1.0
log_step = 10
eval_step = 30
seed = 1000

train_path = './data/train.kbqa.char.bmes'
dev_path = './data/dev.kbqa.char.bmes'
save_path = "/kaggle/working/kbqa_system/checkpoint/"

# envir setting
set_seed(seed)
use_gpu = True if paddle.get_device().startswith("gpu") else False
if use_gpu:
    paddle.set_device("gpu:0")

if __name__ == '__main__':
    pass
    # train()

    # pred_model_path = NERConfig().best_model_path
    # input_text = '谁是《全金属狂潮》的色彩设计者？'
    # pred_results = predict(pred_model_path, input_text)
    # print(pred_results)
