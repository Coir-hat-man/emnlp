#nohup bash /data/home/weijh/zyh/emnlp/BE/Baseline/Flash-attn-Autoregressive_Decoding/result/runad.sh > /data/home/weijh/zyh/emnlp/BE/Baseline/Flash-attn-Autoregressive_Decoding/result/output.txt 2>&1 &

PYTHON_SCRIPT="/data/home/weijh/zyh/emnlp/BE/Baseline/Flash-attn-Autoregressive_Decoding/request.py"


# 计算总迭代次数
start=1.0
end=3.4
step=0.4
total_iterations=$(echo "($end - $start)/$step + 1" | bc)
current_iteration=0



echo "开始循环执行Python脚本..."
echo "request_speed范围: $start 到 $end, 步长: $step"
echo "总迭代次数: $total_iterations"
echo ""

for speed in $(seq $start $step $end); do
    # 更新进度
    current_iteration=$((current_iteration + 1))
    progress=$((current_iteration * 100 / total_iterations))
    
    # 绘制进度条
    bar="["
    for ((i=0; i<50; i++)); do
        if [ $i -lt $((progress / 2)) ]; then
            bar+="="
        else
            bar+=" "
        fi
    done
    bar+="]"
    
    # 打印进度信息
    printf "\r%s %d%% | 当前request_speed: %.1f" "$bar" "$progress" "$speed"
    
    # 执行Python脚本并将输出追加到文件
    CUDA_VISIBLE_DEVICES=0 python "$PYTHON_SCRIPT" --Request_speed "$speed" 
    
done

echo -e "\n\n所有迭代完成"