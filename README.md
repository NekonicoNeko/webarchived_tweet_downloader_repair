download_archive.py 替换原版  
解决Windows兼容性问题  
JSON_TIMEOUT改为60解决频繁readtimeout=15的问题  
对上级回复链的查找顺序修改为wayback先于xAPI  
HTML生成已适配下级回复独立归档  
修复x_api_auths为空列表时无法.describe()的问题  
修复HTML将所有带评论树的根推文也显示在“回复”栏中的问题  

————————————————————————————————————————————————————

browser_response_import  
用于将从浏览器response获取的TweetDetailGraphQL数据导入webarchived_tweet_downloader归档

python "browser_response_import.py" username "response.txt"  
支持txt/json/多个文件/文件夹

python "browser_response_import.py" scan username "response.txt"（或--scan）  
只读预览

--exclude-ids tweetid  
排除指定id推文不导入独立json

--no-auto-repair-existing  
不根据导入的上级推文自动修复已存在的下级回复的上级回复链（默认修复）

--repair-existing-context  
修复导入器已导入的推文上级回复链未嵌入本地上级回复的问题（de旧版本的bug用，只有用旧版本导入过的才需要跑，现在已经没这个bug了）

支持--json-only、--workers N、--help
