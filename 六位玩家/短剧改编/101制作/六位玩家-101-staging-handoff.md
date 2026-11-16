# 六位玩家101站位交接：六人全在场最终版

仅101，纠正我们旧presentAtStart=[]错误元数据，不是用户修改剧情。旧空场/单人版本全部作废。六人本就在开场的食堂门口出门过程起点，不能先让六人全在檐下静止后再重复出门，原第1拍一起继续走向檐下，y坤避雨；不写enter，不更改26拍对白。

正式工作数据：D:\work\articles\六位玩家\短剧改编\101制作\六位玩家-101-staging.json
HTML：D:\work\articles\六位玩家\短剧改编\101制作\staging-report.html
局部剧本：D:\work\articles\六位玩家\短剧改编\101制作\六位玩家-101-script.json
美术：D:\work\articles\六位玩家\短剧改编\101制作\六位玩家-101-art.json
大纲：D:\work\articles\六位玩家\短剧改编\六位玩家-outline.json

参考图元数据（已实看，全图单视角）：
{
  "path": "D:\\work\\articles\\六位玩家\\短剧改编\\101制作\\images\\学校食堂门口-sheet.png",
  "width": 1536,
  "height": 864,
  "aspectRatio": "16:9",
  "sha256": "1baf113d3f8eba55cf6ddadd5260f4b624dccf6916ff50c2e84cb925e47a2aae",
  "mainViewBox": [
    0,
    0,
    1,
    1
  ],
  "boxBasis": "full-image"
}

4项认图：双扇玻璃门、檐下空地、两级宽台阶、雨中步道。box相对实际全图，plan独立展平；制作北为门墙侧，不是真实地理北。没有跨场景空间连接。参考图改变后重认。

六人present=true、站立、无群演。主角/森巴在门南侧沿.18/.82；杰哥在门东侧沿.2；x坤/y坤/邓布利多在檐下空地北侧沿.18/.5/.82。不同侧/同侧分散不叠点。afterEntranceDesign为后续移动辅助，不可盖过当前开场。

validate --script 六位玩家-101-script.json --art 六位玩家-101-art.json 返回0，1地点1场全部通过；blocking和render --art均返回0。未浏览器交互测试。没有改技能、全剧剧本或付费调用。

下一步novel-storyboard仅101：先读取本交接及正式JSON，以文件为准保留最新六人开场在场；首段openingPresent=C01至C06，无enter事件。输入缺失或校验未过先定位问题，不扩全剧。用blocking最新命令输出接首段；旧空场句不再用。
