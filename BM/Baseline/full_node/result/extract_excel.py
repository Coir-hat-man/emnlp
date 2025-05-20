import json
import pandas as pd
from openpyxl import Workbook

def process_jsonl_to_excel(input_file, output_file):
    # 读取JSONL文件
    data = []
    with open(input_file, 'r', encoding='utf-8') as f:
        for line in f:
            data.append(json.loads(line))
    
    # 按request_speed从小到大排序
    sorted_data = sorted(data, key=lambda x: x['request_speed'])
    
    # 选择并排序字段，并保留2位小数
    processed_data = []
    for item in sorted_data:
        processed_item = {
            'request_speed': round(item['request_speed'], 2),
            'throughput': round(item['throughput'], 2),
            'average_latency': round(item['average_latency'], 2),
            'TPOT_ratio': round(item['TPOT_ratio'], 2),
            'Accept_ratio': round(item['Accept_ratio'], 2),
            'batch_size': item['batch_size'],  # 整数不需要处理
            'Node_num': item['Node_num']      # 整数不需要处理
        }
        processed_data.append(processed_item)
    
    # 转换为DataFrame
    df = pd.DataFrame(processed_data)
    
    # 写入Excel文件
    df.to_excel(output_file, index=False, engine='openpyxl')
    print(f"数据已成功写入 {output_file}")

# 使用示例
input_jsonl = '/data/home/weijh/zyh/emnlp/BM/ours/result/60output.jsonl'  # 替换为你的输入文件路径
output_excel = '/data/home/weijh/zyh/emnlp/BM/ours/result/60.xlsx'  # 替换为你想要的输出文件路径
process_jsonl_to_excel(input_jsonl, output_excel)