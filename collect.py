#!/usr/bin/env python3


from bs4 import BeautifulSoup
import os
import re
import multiprocessing
import time
import urllib3

# 尝试使用 httpx，它对 SOCKS5 代理支持更好，且能更好地模拟浏览器
try:
    import httpx
    USE_HTTPX = True
    # 检查是否安装了 socksio（SOCKS 代理支持需要）
    try:
        import socksio
        HTTPX_HAS_SOCKS = True
    except ImportError:
        HTTPX_HAS_SOCKS = False
except ImportError:
    import requests
    USE_HTTPX = False
    HTTPX_HAS_SOCKS = False
    # 禁用 SSL 警告（因为使用 SOCKS5 代理时可能出现 SSL 验证问题）
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# 真实的浏览器请求头，模拟 Chrome 浏览器
BROWSER_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
    'Accept-Language': 'en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7',
    'Accept-Encoding': 'gzip, deflate, br',
    'DNT': '1',
    'Connection': 'keep-alive',
    'Upgrade-Insecure-Requests': '1',
    'Sec-Fetch-Dest': 'document',
    'Sec-Fetch-Mode': 'navigate',
    'Sec-Fetch-Site': 'none',
    'Sec-Fetch-User': '?1',
    'Cache-Control': 'max-age=0',
}

'''
Query https://syzkaller.appspot.com/upstream for all bugs against upstream kernel and have "C" and "syz" reproducers
Save reproducers to text files
'''

# SOCKS5 代理配置，从环境变量读取，格式: socks5://127.0.0.1:1080 或 socks5h://127.0.0.1:1080
# socks5h 表示通过代理解析 DNS
PROXY = os.environ.get('SOCKS5_PROXY', None)
if PROXY:
    # requests 的代理配置格式
    proxies = {
        'http': PROXY,
        'https': PROXY
    }
    # httpx 需要单独的代理配置（格式相同）
    httpx_proxy = PROXY
else:
    proxies = None
    httpx_proxy = None

# 文件保存路径，从环境变量读取，默认为当前目录下的 files/
SAVE_DIR = os.environ.get('SAVE_DIR', 'files')

# 测试模式：从环境变量读取，如果设置为 "true" 或 "1"，则只处理前 10 个 bug
TEST_MODE = os.environ.get('TEST_MODE', '').lower() in ('true', '1', 'yes')
TEST_LIMIT = 10  # 测试模式下只处理前 10 个 bug（按 Rank 排序后的前 10 个）


class ResponseWrapper:
    """
    将 httpx.Response 包装为类似 requests.Response 的接口
    用于统一 httpx 和 requests 的返回对象
    """
    def __init__(self, httpx_response):
        self.content = httpx_response.content
        self.text = httpx_response.text
        self.status_code = httpx_response.status_code
        self._httpx_response = httpx_response
    
    def raise_for_status(self):
        """如果状态码 >= 400，抛出异常"""
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"{self.status_code} {self._httpx_response.reason_phrase}",
                request=self._httpx_response.request,
                response=self._httpx_response
            )


def create_httpx_client(proxy=None):
    """
    创建并返回 httpx.Client 实例
    
    Args:
        proxy: SOCKS5 代理地址，格式如 socks5://127.0.0.1:1080
    
    Returns:
        httpx.Client 实例
    """
    client_kwargs = {
        'timeout': 90.0,
        'verify': False,
        'headers': BROWSER_HEADERS,
        'follow_redirects': True
    }
    if proxy:
        client_kwargs['proxy'] = proxy
    return httpx.Client(**client_kwargs)


def make_httpx_request(url, proxy=None):
    """
    使用 httpx 发送 HTTP GET 请求
    
    Args:
        url: 请求的 URL
        proxy: SOCKS5 代理地址，格式如 socks5://127.0.0.1:1080
    
    Returns:
        ResponseWrapper 对象，兼容 requests.Response 接口
    """
    with create_httpx_client(proxy) as client:
        response = client.get(url)
        response.raise_for_status()
        return ResponseWrapper(response)


def make_requests_request(url, proxies=None):
    """
    使用 requests 发送 HTTP GET 请求
    
    Args:
        url: 请求的 URL
        proxies: 代理配置字典，格式如 {'http': 'socks5://...', 'https': 'socks5://...'}
    
    Returns:
        requests.Response 对象
    """
    response = requests.get(
        url,
        proxies=proxies,
        timeout=90,
        verify=False,
        headers=BROWSER_HEADERS
    )
    response.raise_for_status()
    return response

def rate_limited_get(url, delay=1.0, max_retries=5):
    """
    带限流和重试机制的 HTTP GET 请求函数
    
    Args:
        url: 请求的 URL
        delay: 请求之间的最小延迟（秒）
        max_retries: 最大重试次数
    
    Returns:
        Response 对象（httpx 或 requests）
    """
    global last_request_time, proxies, httpx_proxy, USE_HTTPX
    retry_count = 0
    
    while retry_count < max_retries:
        # 限流控制
        while True:
            with request_lock:
                now = time.time()
                elapsed = now - last_request_time.value
                if elapsed >= delay:
                    last_request_time.value = time.time()
                    break
            time.sleep(max(0, delay - elapsed))  # Sleep the remaining time if any
        
        try:
            # 根据配置选择使用 httpx 或 requests
            if USE_HTTPX:
                return make_httpx_request(url, httpx_proxy)
            else:
                return make_requests_request(url, proxies)
        except Exception as e:
            # 统一处理所有异常
            is_timeout = False
            is_429 = False
            status_code = None
            
            # 检查异常类型
            if USE_HTTPX:
                if isinstance(e, httpx.TimeoutException):
                    is_timeout = True
                elif isinstance(e, httpx.HTTPStatusError):
                    status_code = e.response.status_code if hasattr(e, 'response') else None
                    if status_code == 429:
                        is_429 = True
                elif isinstance(e, httpx.RequestError):
                    pass  # 其他网络错误
            else:
                if isinstance(e, requests.exceptions.Timeout):
                    is_timeout = True
                elif isinstance(e, requests.exceptions.HTTPError):
                    status_code = e.response.status_code if hasattr(e, 'response') else None
                    if status_code == 429:
                        is_429 = True
                elif isinstance(e, requests.exceptions.RequestException):
                    pass  # 其他网络错误
            
            # 处理超时错误
            if is_timeout:
                retry_count += 1
                if retry_count < max_retries:
                    wait_time = delay * (2 ** retry_count)  # 指数退避
                    print(f"请求超时 {url}，等待 {wait_time:.1f} 秒后重试 ({retry_count}/{max_retries})...", flush=True)
                    time.sleep(wait_time)
                    continue
                else:
                    print(f"请求超时 {url}: {e} (已重试 {max_retries} 次)", flush=True)
                    raise
            
            # 处理 429 错误
            if is_429:
                retry_count += 1
                if retry_count < max_retries:
                    wait_time = delay * (2 ** retry_count) * 2  # 额外等待时间
                    print(f"遇到 429 错误，等待 {wait_time:.1f} 秒后重试 ({retry_count}/{max_retries})...", flush=True)
                    time.sleep(wait_time)
                    continue
                else:
                    print(f"请求失败 {url}: {e} (已重试 {max_retries} 次)", flush=True)
                    raise
            
            # 处理其他 HTTP 错误
            if status_code and status_code != 429:
                print(f"请求失败 {url}: {e}", flush=True)
                raise
            
            # 检查是否是缺少 socksio 的错误（不应该重试，直接报错）
            error_msg = str(e).lower()
            if 'socksio' in error_msg or ('socks' in error_msg and 'not installed' in error_msg):
                print(f"❌ 请求失败 {url}：缺少 SOCKS 代理支持")
                print(f"   错误详情: {e}")
                print(f"   请运行: pip install httpx[socks]")
                raise
            
            # 处理其他网络错误（重试）
            retry_count += 1
            if retry_count < max_retries:
                wait_time = delay * (2 ** retry_count)
                error_type = "网络错误" if (USE_HTTPX and isinstance(e, httpx.RequestError)) or (not USE_HTTPX and isinstance(e, requests.exceptions.RequestException)) else "未知错误"
                print(f"{error_type} {url}: {e}，等待 {wait_time:.1f} 秒后重试 ({retry_count}/{max_retries})...", flush=True)
                time.sleep(wait_time)
                continue
            else:
                print(f"请求失败 {url}: {e} (已重试 {max_retries} 次)", flush=True)
                raise
    
    raise Exception(f"请求失败 {url}: 达到最大重试次数")

def init_worker(lock, last_time, proxy_config, httpx_proxy_config, save_dir, file_counter):
    """Initialize global variables on the worker process."""
    global request_lock, last_request_time, proxies, httpx_proxy, SAVE_DIR, downloaded_file_count
    request_lock = lock
    last_request_time = last_time
    proxies = proxy_config
    httpx_proxy = httpx_proxy_config
    SAVE_DIR = save_dir
    downloaded_file_count = file_counter

def get_reproducers(bug_info):
        """
        bug_info 是一个元组: (bug_link, rank)
        """
        global SAVE_DIR, downloaded_file_count, TEST_MODE, TEST_LIMIT, USE_HTTPX
        try:
            # 测试模式：检查是否已达到文件数量限制
            if TEST_MODE:
                with downloaded_file_count.get_lock():
                    if downloaded_file_count.value >= TEST_LIMIT:
                        return  # 已达到测试模式文件数量限制
            bug_link, rank = bug_info
            # get the id from the bug
            bug_id = bug_link.split("=")[1]
            existing_files = [f for f in os.listdir(SAVE_DIR) if f.startswith(bug_id)]
            if existing_files:
                    return  # 静默跳过已存在的文件
            page = rate_limited_get("https://syzkaller.appspot.com" + bug_link)
            soup = BeautifulSoup(page.content, 'html.parser')
            
            # 找到包含 "Syz repro" 列的表格
            tables = soup.find_all('table', class_="list_table")
            if not tables:
                return  # 静默跳过没有表格的 bug
            
            # 找到最后一个表格（通常是 reproducers 表格）
            table = tables[-1]
            
            # 找到表头，确定 "Syz repro" 列的索引
            headers = table.find('thead')
            if not headers:
                return  # 静默跳过没有表头的 bug
            
            header_row = headers.find('tr')
            if not header_row:
                return  # 静默跳过没有表头行的 bug
            
            # 找到 "Syz repro" 列的索引
            header_cells = header_row.find_all('th')
            syz_repro_col_idx = None
            for idx, th in enumerate(header_cells):
                if 'Syz repro' in th.get_text():
                    syz_repro_col_idx = idx
                    break
            
            if syz_repro_col_idx is None:
                return  # 静默跳过没有 'Syz repro' 列的 bug
            
            # 遍历表格行，在 "Syz repro" 列中查找包含 "syz" 的链接
            tbody = table.find('tbody')
            if not tbody:
                tbody = table  # 如果没有 tbody，直接使用 table
            
            rows = tbody.find_all('tr')
            found_syz = False
            for row in rows:
                cells = row.find_all('td')
                if len(cells) <= syz_repro_col_idx:
                    continue
                
                # 获取 "Syz repro" 列的单元格
                syz_repro_cell = cells[syz_repro_col_idx]
                syz_repro_text = syz_repro_cell.get_text().lower()
                
                # 检查是否包含 "syz"（可能是 "syz / log" 或其他格式）
                if 'syz' in syz_repro_text:
                    # 在该单元格中查找所有链接
                    links = syz_repro_cell.find_all('a')
                    for link in links:
                        try:
                            href = link.get('href')
                            if not href:
                                continue
                            
                            # 只下载 tag=ReproSyz 的链接，绝对不下载 tag=ReproLog
                            # 首先明确排除 ReproLog，确保绝对不会下载
                            if 'tag=ReproLog' in href:
                                continue  # 跳过所有 ReproLog 链接，绝不下载
                            
                            # 只处理 tag=ReproSyz 的链接
                            if 'tag=ReproSyz' in href:
                                # 下载这个链接的内容
                                page = rate_limited_get("https://syzkaller.appspot.com" + href)
                                # 从链接中提取 x 参数
                                x_match = re.search(r'x=([^&]+)', href)
                                if x_match:
                                    x = x_match.group(1)
                                else:
                                    # 如果没有 x 参数，使用链接的一部分作为标识
                                    x = re.sub(r'[^a-zA-Z0-9]', '_', href[-20:])
                                
                                # 测试模式：在保存前检查是否已达到文件数量限制
                                if TEST_MODE:
                                    with downloaded_file_count.get_lock():
                                        if downloaded_file_count.value >= TEST_LIMIT:
                                            return  # 已达到测试模式文件数量限制
                                
                                filepath = os.path.join(SAVE_DIR, f"{bug_id}-{x}.txt")
                                
                                # 测试模式：保存文件后增加计数器
                                if TEST_MODE:
                                    with downloaded_file_count.get_lock():
                                        if downloaded_file_count.value >= TEST_LIMIT:
                                            return  # 再次检查，避免并发问题
                                        downloaded_file_count.value += 1
                                        current_count = downloaded_file_count.value
                                        print(f"保存 bug {bug_id} (Rank: {rank}) 的 reproducer (x={x}) 到 {filepath} [{current_count}/{TEST_LIMIT}]")
                                else:
                                    print(f"保存 bug {bug_id} (Rank: {rank}) 的 reproducer (x={x}) 到 {filepath}")
                                
                                with open(filepath, 'w+', encoding='utf-8') as f:
                                    f.write(page.text)
                                found_syz = True
                                
                                # 测试模式：保存后再次检查是否达到限制
                                if TEST_MODE:
                                    with downloaded_file_count.get_lock():
                                        if downloaded_file_count.value >= TEST_LIMIT:
                                            return  # 已达到限制，停止处理
                        except Exception as e:
                            print(f"处理 bug {bug_id} (Rank: {rank}) 的 reproducer 链接时出错: {e}", flush=True)
                            continue
            
            # 如果没有找到 syz 链接，静默跳过
        except Exception as e:
            # 统一处理所有异常
            error_str = str(e).lower()
            is_timeout = "timeout" in error_str
            is_429 = "429" in error_str or (hasattr(e, 'response') and hasattr(e.response, 'status_code') and e.response.status_code == 429)
            
            if is_timeout:
                # 超时错误会在 rate_limited_get 中重试，如果最终失败会在这里捕获
                print(f"处理 bug {bug_info[0]} 时超时: {e}", flush=True)
            elif is_429:
                # 429 错误会在 rate_limited_get 中处理，这里只记录
                pass
            elif USE_HTTPX and isinstance(e, (httpx.RequestError, httpx.HTTPStatusError)):
                # httpx 网络相关错误
                print(f"处理 bug {bug_info[0]} 时网络错误: {e}", flush=True)
            elif not USE_HTTPX and isinstance(e, (requests.exceptions.RequestException, requests.exceptions.HTTPError)):
                # requests 网络相关错误
                print(f"处理 bug {bug_info[0]} 时网络错误: {e}", flush=True)
            else:
                # 其他错误
                print(f"处理 bug {bug_info[0]} 时出错: {e}", flush=True)

def parse_bugs_from_page(url, page_name):
    """解析页面，提取符合条件的 bug 信息，返回 (bug_link, rank) 列表"""
    global USE_HTTPX, httpx_proxy, proxies
    bugs = []
    try:
        # 使用统一的请求函数获取页面内容
        if USE_HTTPX:
            response = make_httpx_request(url, httpx_proxy)
        else:
            response = make_requests_request(url, proxies)
        page_content = response.content
        
        soup = BeautifulSoup(page_content, 'html.parser')
        
        # 找到所有 list_table 表格，upstream/ 页面有两个表格，需要解析第二个（open 部分下的表格）
        all_tables = soup.find_all('table', class_='list_table')
        if not all_tables:
            print(f"{page_name} 页面没有找到表格")
            return bugs
        
        # 如果是 upstream/ 页面，使用第二个表格；否则使用第一个表格
        if 'upstream' in url and len(all_tables) >= 2:
            table = all_tables[1]  # 第二个表格（open 部分）
            print(f"{page_name} 页面找到 {len(all_tables)} 个表格，使用第二个表格（open 部分）")
        else:
            table = all_tables[0]  # 第一个表格
            print(f"{page_name} 页面找到 {len(all_tables)} 个表格，使用第一个表格")
        
        # 找到表头，确定各列的索引
        thead = table.find('thead')
        if not thead:
            print(f"{page_name} 页面没有找到表头")
            return bugs
        
        header_row = thead.find('tr')
        if not header_row:
            print(f"{page_name} 页面没有找到表头行")
            return bugs
        
        header_cells = header_row.find_all('th')
        rank_col_idx = None
        repro_col_idx = None
        title_col_idx = None
        
        for idx, th in enumerate(header_cells):
            text = th.get_text().strip()
            if 'Rank' in text:
                rank_col_idx = idx
            elif 'Repro' in text:
                repro_col_idx = idx
            elif 'Title' in text or 'Bug' in text:
                title_col_idx = idx
        
        if rank_col_idx is None or repro_col_idx is None or title_col_idx is None:
            print(f"{page_name} 页面列索引解析失败 (Rank: {rank_col_idx}, Repro: {repro_col_idx}, Title: {title_col_idx})")
            return bugs
        
        # 解析表格行
        tbody = table.find('tbody')
        if not tbody:
            tbody = table
        
        rows = tbody.find_all('tr')
        for row in rows:
            cells = row.find_all('td')
            if len(cells) <= max(rank_col_idx, repro_col_idx, title_col_idx):
                continue
            
            # 获取 Rank
            rank_cell = cells[rank_col_idx]
            try:
                rank = int(rank_cell.get_text().strip())
            except (ValueError, AttributeError):
                rank = 0
            
            # 获取 Repro 列，检查是否包含 C 或 syz
            repro_cell = cells[repro_col_idx]
            repro_text = repro_cell.get_text()
            has_c = False
            has_syz = False
            
            # 检查 stat 单元格（可能在 repro_cell 内部，也可能是并列的）
            # 首先检查 repro_cell 内部的 stat 单元格
            stat_cells = repro_cell.find_all('td', class_='stat')
            # 如果没有找到，检查整个行中的 stat 单元格（可能在同一行但不同列）
            if not stat_cells:
                # 检查整个单元格的文本
                if 'C' in repro_text:
                    has_c = True
                if 'syz' in repro_text.lower():
                    has_syz = True
            else:
                # 检查所有 stat 单元格
                for stat_cell in stat_cells:
                    stat_text = stat_cell.get_text().strip()
                    if stat_text == 'C':
                        has_c = True
                    elif stat_text.lower() == 'syz':
                        has_syz = True
            
            # 如果还没找到，检查整个单元格的文本内容（可能 stat 单元格是 span 或其他标签）
            if not (has_c or has_syz):
                # 检查所有可能的 stat 元素
                for stat_elem in repro_cell.find_all(class_='stat'):
                    stat_text = stat_elem.get_text().strip()
                    if stat_text == 'C':
                        has_c = True
                    elif stat_text.lower() == 'syz':
                        has_syz = True
                
                # 最后检查纯文本
                if not has_c and 'C' in repro_text:
                    has_c = True
                if not has_syz and 'syz' in repro_text.lower():
                    has_syz = True
            
            # 只要有 C 或 syz 就添加
            if has_c or has_syz:
                # 获取 bug 链接
                title_cell = cells[title_col_idx]
                link = title_cell.find('a')
                if link:
                    bug_link = link.get('href')
                    bugs.append((bug_link, rank))
        
        return bugs
    except Exception as e:
        error_msg = str(e)
        # 检查是否是缺少 socksio 的错误
        if 'socksio' in error_msg.lower() or 'socks' in error_msg.lower() and 'not installed' in error_msg.lower():
            print(f"❌ 解析 {page_name} 页面失败：缺少 SOCKS 代理支持")
            print(f"   错误详情: {e}")
            print(f"   请运行: pip install httpx[socks]")
        else:
            print(f"解析 {page_name} 页面失败: {e}")
        return bugs

def main():
    # 打印配置信息（只在主进程中打印一次）
    global PROXY, SAVE_DIR, TEST_MODE, TEST_LIMIT, USE_HTTPX, HTTPX_HAS_SOCKS
    if PROXY:
        print(f"使用 SOCKS5 代理: {PROXY}")
        # 检查 httpx 是否支持 SOCKS
        if USE_HTTPX and not HTTPX_HAS_SOCKS:
            print("❌ 错误：httpx 已安装，但缺少 SOCKS 代理支持！")
            print("   请运行以下命令安装：pip install httpx[socks]")
            print("   或者使用 requests 库（需要安装 requests[socks]）")
            return
    else:
        print("未设置代理，使用直连")
    
    if USE_HTTPX:
        if HTTPX_HAS_SOCKS:
            print("使用 httpx 库（已支持 SOCKS5 代理）")
        else:
            print("使用 httpx 库（未安装 SOCKS 支持，如需使用代理请运行：pip install httpx[socks]）")
    else:
        print("使用 requests 库（建议安装 httpx 以获得更好的 SOCKS5 支持：pip install httpx[socks]）")
    
    if TEST_MODE:
        print(f"⚠️  测试模式已启用：只处理前 {TEST_LIMIT} 个 bug")
    else:
        print("正常模式：处理所有符合条件的 bug")
    
    if not os.path.exists(SAVE_DIR):
        os.makedirs(SAVE_DIR)
        print(f"创建保存目录: {SAVE_DIR}")
    else:
        print(f"使用保存目录: {SAVE_DIR}")
    
    # Query the page
    bugs = []
    print("正在获取当前 bug 列表（按 Rank 排序）...")
    bugs.extend(parse_bugs_from_page("https://syzkaller.appspot.com/upstream/", "upstream/"))
    print(f"从 upstream/ 页面找到 {len(bugs)} 个符合条件的 bug")
    
    print("正在获取已修复 bug 列表（按 Rank 排序）...")
    fixed_bugs = parse_bugs_from_page("https://syzkaller.appspot.com/upstream/fixed", "upstream/fixed")
    # 避免重复
    existing_links = {bug[0] for bug in bugs}
    for bug_info in fixed_bugs:
        if bug_info[0] not in existing_links:
            bugs.append(bug_info)
    print(f"从 upstream/fixed 页面找到 {len(fixed_bugs)} 个符合条件的 bug")
    
    # 按 Rank 从高到低排序
    bugs.sort(key=lambda x: x[1], reverse=True)
    
    if len(bugs) == 0:
        print("没有找到任何符合条件的 bug，退出")
        return
    
    # 测试模式：提示信息（实际限制在文件下载时进行）
    if TEST_MODE:
        print(f"\n⚠️  测试模式：将从所有 {len(bugs)} 个 bug 中下载最多 {TEST_LIMIT} 个文件")
    
    # 统计每个 rank 的 bug 数量
    rank_count = {}
    for bug_link, rank in bugs:
        rank_count[rank] = rank_count.get(rank, 0) + 1
    
    print(f"\n总共找到 {len(bugs)} 个符合条件的 bug，按 Rank 从高到低排序")
    print("各 Rank 的 bug 数量统计:")
    for rank in sorted(rank_count.keys(), reverse=True):
        print(f"  Rank {rank}: {rank_count[rank]} 个")
    print()
    
    # for each bug, get the reproducers from "https://syzkaller.appspot.com/$bug"
    # run the following code in 5 parallel processes (进一步降低并发数，避免 SOCKS5 代理 SSL 错误)
    lock = multiprocessing.Lock()
    last_time = multiprocessing.Value('d', time.time())
    file_counter = multiprocessing.Value('i', 0)  # 用于测试模式：跟踪已下载的文件数量
    pool = multiprocessing.Pool(16, initializer=init_worker, initargs=(lock, last_time, proxies, httpx_proxy, SAVE_DIR, file_counter))
    pool.map(get_reproducers, bugs)
    pool.close()
    pool.join()
    print("所有任务完成！")

if __name__ == "__main__":
    main()
