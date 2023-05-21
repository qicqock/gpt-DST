import os
os.environ["TOKENIZERS_PARALLELISM"] = "false"
import json
import argparse
import copy
import time
from collections import defaultdict
from tqdm import tqdm
from utils.helper import SpeedLimitTimer, PreviousStateRecorder
from utils.typo_fix import typo_fix
from config import CONFIG

from api_request.gpt35_turbo_completion import gpt35_turbo_completion
from utils.our_parse import sv_dict_to_string, our_pred_parse, our_pred_parse_with_bracket, slot_classify_parse, pred_parse_with_bracket_matching
from prompt.our_prompting import conversion, get_our_prompt, custom_prompt, get_prompt_with_bracket, get_full_history_prompt, get_excluded_history_prompt,\
 get_slot_classify_prompt, slot_classify_prompt, slot_description_prompt
from retriever.code.embed_based_retriever import EmbeddingRetriever
from evaluate.evaluate_metrics import evaluate
from evaluate.evaluate_FGA import FGA

train_fn = 'data/mw21_5p_train_v2.json'
retriever_dir = 'retriever/expts/mw21_5p_v2'
output_file_name = 'gpt35_mw24_5p_v2_aaaaaaaaaa'
mwz_ver = 2.4
test_fn = ''
test_size = 10

# current time
cur_time = time.strftime('%y%m%d_%H%M-')

# create the output folder
output_dir = 'expts/' + cur_time + output_file_name
os.makedirs(output_dir, exist_ok=True)

NUM_EXAMPLE=1

# read the selection pool
with open(train_fn) as f:
    train_set = json.load(f)

# read the ontology and the test set
if mwz_ver == '2.1':
    ontology_path = CONFIG["ontology_21"]
    if test_fn == "":
        test_set_path = "./data/mw21_100p_test.json"
else:
    ontology_path = CONFIG["ontology_24"]
    if test_fn == "":
        test_set_path = "./data/mw24_100p_test.json"

# evaluate on some other file
if test_fn:
    test_set_path = test_fn

with open(ontology_path) as f:
    ontology = json.load(f)
with open(test_set_path) as f:
    test_set = json.load(f)

retriever = EmbeddingRetriever(datasets=[train_set],
                               model_path=retriever_dir,
                               search_index_filename=os.path.join(retriever_dir, "train_index.npy"), 
                               sampling_method="pre_assigned")



def run(test_set, turn=-1, use_gold=False):
    # turn and use_gold are for analysis purpose
    # turn = -1 means evalute all dialogues
    # turn = 0 means evaluate single-turn dialogues
    # turn = 1 means evalute two-turn dialogues... etc.
    # when use_gold = True, the context are gold context (for analysis purpose)

    timer = SpeedLimitTimer(second_per_step=3.1)  # openai limitation 20 queries/min

    result_dict = defaultdict(list)  # use to record the accuracy

    selected_set = test_set
    # if needed, only evaluate on particular turns (analysis purpose)
    if turn >= 0:
        if not use_gold:
            raise ValueError("can only evaluate particular turn when using gold context")
        selected_set = [d for d in test_set if len(d['dialog']['usr']) == turn + 1]
    
    prediction_recorder = PreviousStateRecorder()  # state recorder

    # start experiment
    all_result = []
    n_total = 0
    n_correct = 0
    total_acc = 0
    total_f1 = 0

    # specify ontology_prompt, prompt_function
    ontology_prompt = custom_prompt
    get_prompt = get_excluded_history_prompt
    our_parse = pred_parse_with_bracket_matching
    mode = "with_bracket"

    print("********* "+ mode + " *********\n")

    for data_idx, data_item in enumerate(tqdm(selected_set)):
        n_total += 1

        completion = ""
        if use_gold:
            prompt_text = get_prompt(
                data_item, examples=retriever.item_to_nearest_examples(data_item, k=NUM_EXAMPLE))
        else:
            predicted_context = prediction_recorder.state_retrieval(data_item)
            modified_item = copy.deepcopy(data_item)
            modified_item['last_slot_values'] = predicted_context

            examples = retriever.item_to_nearest_examples(
                modified_item, k=NUM_EXAMPLE)
            
            prompt_text = get_prompt(
                data_item, examples=examples, given_context=predicted_context)

        # print(prompt_text.replace(conversion(ontology_prompt), ""))

        # record the prompt
        data_item['prompt'] = prompt_text

        # prompt 확인용
        print(prompt_text)
        # continue

        # gpt35 completion
        complete_flag = False
        parse_error_count = 0
        while not complete_flag:
            try:
                # completion = gpt35_completion(prompt_text)
                # completion = ada_completion(prompt_text)
                # completion = babbage_completion(prompt_text)
                completion = gpt35_turbo_completion(prompt_text)
                completion = conversion(completion, reverse=True)
            except Exception as e:
                if e.user_message.startswith("This model's maximum context length"):
                    print("prompt overlength")
                    examples = examples[1:]
                    prompt_text = get_prompt(
                        data_item, examples=examples, given_context=predicted_context)
                else:
                    # throughput too high
                    timer.sleep(10)
            else:
                try:
                    # check if CODEX is crazy 
                    temp_parse = our_parse(completion)
                except:
                    print("parse error")
                    print("generate completion again...")
                    parse_error_count += 1
                    if parse_error_count >= 3:
                        print("exceed parse error limit... exit")
                        complete_flag = True
                else:
                    complete_flag = True
            # limit query speed
            timer.step()

        # aggregate the prediction and the history states
        predicted_slot_values = {}
        try:
            predicted_slot_values = our_parse(completion) # a dictionary
        except:
            print("the output is not a valid result")
            data_item['not_valid'] = 1

        predicted_slot_values = typo_fix(predicted_slot_values, ontology=ontology, version=mwz_ver)

        context_slot_values = data_item['last_slot_values']  # a dictionary

        # merge context and prediction
        if use_gold:
            all_slot_values = context_slot_values.copy()
        else:
            all_slot_values = prediction_recorder.state_retrieval(
                data_item).copy()

        for s, v in predicted_slot_values.items():
            if s in all_slot_values and v == "[DELETE]":
                del all_slot_values[s]
            elif v != "[DELETE]":
                all_slot_values[s] = v

        # some slots may contain multiple values
        all_slot_values = {k: v.split('|')[0] for k, v in all_slot_values.items()}
        
        prediction_recorder.add_state(data_item, all_slot_values)

        # record the predictions
        data_item['pred'] = all_slot_values
        data_item['ontology_path'] = ontology_path
        data_item['completion'] = completion
        data_item['predicted_slot_values'] = predicted_slot_values

        # print the result
        print(completion)
        print(f"this is the {n_total - 1}th example. {data_item['ID']}_turn_{data_item['turn_id']}")
        print(f"pred turn change: {sv_dict_to_string(predicted_slot_values, sep='-')}")
        print(f"gold turn change: {sv_dict_to_string(data_item['turn_slot_values'], sep='-')}")
        print(f"pred states: {sv_dict_to_string(all_slot_values, sep='-')}")
        print(f"gold states: {sv_dict_to_string(data_item['slot_values'], sep='-')}")

        this_jga, this_acc, this_f1 = evaluate(all_slot_values,data_item['slot_values'])
        total_acc += this_acc
        total_f1 += this_f1

        if this_jga:
            n_correct += 1
            result_dict[data_item['turn_id']].append(1)
            print("\n=====================correct!=======================")
        else:
            result_dict[data_item['turn_id']].append(0)
            print("\n=====================wrong!=======================")

        # save result
        data_item['JGA'] = n_correct / n_total
        data_item['SA'] = total_acc / n_total
        data_item['Joint_F1'] = total_f1 / n_total
        data_item['pred_status'] = 'correct' if this_jga else 'wrong'

        all_result.append(data_item)

        # Log Checkpoint
        if data_idx % 5 == 0:
            with open(os.path.join(output_dir,f'running_log.json'),'w') as f:
                json.dump(all_result, f, indent=4)

        print("\n\n\n####################################################################################################################\n\n\n")

    print(f"correct {n_correct}/{n_total}  =  {n_correct / n_total}")
    print(f"Slot Acc {total_acc/n_total}")
    print(f"Joint F1 {total_f1/n_total}")
    print()

    # calculate the accuracy of each turn
    for k, v in result_dict.items():
        print(f"accuracy of turn {k} is {sum(v)}/{len(v)} = {sum(v) / len(v)}")

    # save score in score.txt
    with open(os.path.join(output_dir, "score.txt"), 'w') as f:
        f.write(f"correct {n_correct}/{n_total}  =  {n_correct / n_total}\n")
        f.write(f"Slot Acc {total_acc/n_total}\n")
        f.write(f"Joint F1 {total_f1/n_total}\n")
         # calculate the accuracy of each turn
        for k, v in result_dict.items():
            f.write(f"accuracy of turn {k} is {sum(v)}/{len(v)} = {sum(v) / len(v)}\n")

    return all_result






if __name__ == "__main__":

    # api 사용량 위해 개수 제한
    limited_set = test_set[:test_size]
    all_results = run(limited_set)

    with open(os.path.join(output_dir, "running_log.json"), 'w') as f:
        json.dump(all_results, f, indent=4)
    
    print(f"End time: {time.strftime('%y%m%d_%H%M')}")