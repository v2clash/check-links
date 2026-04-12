import asyncio
import aiohttp
import re
import base64
import os
import logging
from urllib.parse import quote

# === 配置日志 ===
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# === 核心配置 ===
GITHUB_TOKEN = os.getenv("BOT")
HEADERS = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github.v3+json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
} if GITHUB_TOKEN else {"User-Agent": "Mozilla/5.0"}

CONCURRENCY_LIMIT = 10  # 最大同时抓取任务数
MAX_SEARCH_PAGES = 3    # GitHub 搜索深度

# 1. 外部源 (Gist)
env_gist = os.getenv("GIST_LINKS", "")
gist_list = [link.strip() for link in env_gist.split(",") if link.strip()]

# 2. 黑名单 (过滤低质量或广告源)
BLACKLIST_USERS = ["moneyfly1", "qjlxg"]
BLACKLIST_KEYWORDS = [
    "kuaidog", "louwangzhiyu", "website", "dashuai", "xship.top", "githubusercontent", "jsdelivr", 
    "trojan vless", "multiserveradelshoop.com", "mojie.best", "tinnyrick8888", "zybs", 
    "vvs.e54.site", "ninecloud", "shanhai", "dmhy.org", "netlify.app", "netlify.com"
]

# 3. 匹配正则
SUB_PROTOCOL_RE = re.compile(r'(vmess|vless|ss|ssr|trojan|clash|tuic|hysteria)://[^\s]+', re.I)
HTTP_LINK_RE = re.compile(r'https?://[^\s]+', re.I)
# 识别可能包含节点列表的订阅 API 关键字
PROXY_KEYWORDS = ["subscribe", "token", "api", "/s/", "/sub", "raw", "v2ray", "clash"]

def safe_base64_decode(data):
    """安全解码 Base64，处理填充和特殊换行"""
    try:
        # 清理空格和换行
        clean_data = "".join(data.split())
        # 补齐长度
        missing_padding = len(clean_data) % 4
        if missing_padding:
            clean_data += '=' * (4 - missing_padding)
        return base64.b64decode(clean_data).decode('utf-8', errors='ignore')
    except:
        return ""

async def fetch_url(session, url, sem):
    """抓取 URL 并解析节点信息"""
    if any(key in url.lower() for key in BLACKLIST_KEYWORDS):
        return ""

    # 自动转换 GitHub URL 为 Raw 原始连接
    target_url = url
    if "gist.github.com" in url and "/raw" not in url:
        target_url = url.rstrip('/') + '/raw'
    elif "github.com" in url and "raw.githubusercontent.com" not in url and "/raw/" not in url:
        if "/blob/" in url:
            target_url = url.replace("github.com", "raw.githubusercontent.com").replace("/blob/", "/")

    async with sem:
        try:
            async with session.get(target_url, timeout=15) as resp:
                if resp.status != 200:
                    return ""
                
                content = await resp.text()
                
                # 尝试解码 Base64 (很多订阅是全 Base64 编码的)
                decoded = safe_base64_decode(content)
                # 判断解码后是否包含协议头，如果不包含则使用原文
                process_text = decoded if SUB_PROTOCOL_RE.search(decoded) else content
                
                extracted = []
                for line in process_text.splitlines():
                    line = line.strip()
                    if not line or "<html>" in line.lower():
                        continue
                    
                    # 仅保留匹配协议且不在黑名单的行
                    if SUB_PROTOCOL_RE.search(line):
                        if not any(k in line.lower() for k in BLACKLIST_KEYWORDS):
                            extracted.append(line)
                return "\n".join(extracted)
        except:
            return ""

async def search_github_precise(session):
    """精确搜索指定路径 data/subscribes.txt"""
    if not GITHUB_TOKEN:
        logger.warning("未配置 GITHUB_TOKEN，搜索功能将受到严格限制。")
    
    found_urls = set()
    # 构造查询：路径包含 data 且 文件名为 subscribes.txt
    query = "path:data+filename:subscribes.txt"
    
    for page in range(1, MAX_SEARCH_PAGES + 1):
        url = f"https://api.github.com/search/code?q={query}&page={page}"
        try:
            async with session.get(url, headers=HEADERS) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    items = data.get('items', [])
                    if not items:
                        break
                    
                    for item in items:
                        owner = item.get('repository', {}).get('owner', {}).get('login', '').lower()
                        if owner not in BLACKLIST_USERS:
                            found_urls.add(item['html_url'])
                    
                    logger.info(f"🔎 GitHub 搜索第 {page} 页: 发现 {len(items)} 个源")
                    # Search API 频率限制较严，加点延迟
                    await asyncio.sleep(1.5)
                elif resp.status == 403:
                    logger.warning("⚠️ 触发 GitHub Search API 速率限制，停止搜索。")
                    break
                else:
                    break
        except Exception as e:
            logger.error(f"❌ 搜索出错: {e}")
            break
    return found_urls

async def main():
    sem = asyncio.Semaphore(CONCURRENCY_LIMIT)
    
    async with aiohttp.ClientSession() as session:
        logger.info("🛠️ 开始执行精确抓取任务...")
        
        # 1. 收集所有目标 URL
        # A. GitHub 精确搜索
        github_urls = await search_github_precise(session)
        # B. 环境变量 Gist
        gists = set(gist_list)
        # C. 本地种子文件
        local_urls = set()
        if os.path.exists('subscribes.txt'):
            with open('subscribes.txt', 'r', encoding='utf-8') as f:
                local_urls = set(HTTP_LINK_RE.findall(f.read()))
        
        all_targets = github_urls | gists | local_urls
        logger.info(f"📋 汇总待抓取源: {len(all_targets)} 个")
        
        # 2. 并发执行抓取
        tasks = [fetch_url(session, url, sem) for url in all_targets]
        results = await asyncio.gather(*tasks)
        
        # 3. 处理结果
        all_nodes = []
        for r in results:
            if r:
                all_nodes.extend(r.splitlines())
        
        # 保持顺序去重
        unique_nodes = list(dict.fromkeys(filter(None, all_nodes)))
        
        # 4. 保存
        with open("filter_subs.txt", "w", encoding="utf-8") as f:
            f.write("\n".join(unique_nodes))
            
        logger.info("-" * 30)
        logger.info(f"✨ 抓取完成！去重后获得 {len(unique_nodes)} 个代理节点")
        logger.info(f"💾 结果已保存至: filter_subs.txt")

if __name__ == "__main__":
    asyncio.run(main())
