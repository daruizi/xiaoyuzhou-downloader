#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小宇宙播客下载器 (Xiaoyuzhou Podcast Downloader)

一个通用的播客下载工具，支持：
- 通过 RSS Feed 获取完整的节目列表
- 增量更新：只下载新节目
- 断点续传：跳过已下载的文件
- 自动按日期分类存放
- 支持配置多个播客

作者: Claude Code
版本: 1.0.0
"""

import os
import sys
import json
import time
import re
import argparse
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set
from dataclasses import dataclass, asdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

try:
    import requests
    import xml.etree.ElementTree as ET
except ImportError:
    print("请先安装依赖: pip install -r requirements.txt")
    sys.exit(1)

# Windows 控制台编码处理
if sys.platform == 'win32':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')


@dataclass
class Episode:
    """节目信息"""
    title: str
    audio_url: str
    pub_date: str
    duration: str
    description: str = ""
    guid: str = ""


@dataclass
class DownloadResult:
    """下载结果"""
    episode: Episode
    success: bool
    filepath: str = ""
    error: str = ""
    skipped: bool = False


class Config:
    """配置管理"""

    DEFAULT_CONFIG = {
        "podcasts": [],
        "download_dir": "./downloads",
        "max_workers": 3,
        "retry_times": 3,
        "retry_delay": 5,
        "request_timeout": 30,
        "download_timeout": 120,
        "check_updates": True,
        "auto_update_days": 1,
        "date_format": "YYYY-MM"
    }

    def __init__(self, config_path: str = "config.json"):
        self.config_path = config_path
        self.data = self.load()

    def load(self) -> dict:
        """加载配置文件"""
        if os.path.exists(self.config_path):
            with open(self.config_path, 'r', encoding='utf-8') as f:
                user_config = json.load(f)
                # 合并默认配置
                config = self.DEFAULT_CONFIG.copy()
                config.update(user_config)
                return config
        return self.DEFAULT_CONFIG.copy()

    def save(self):
        """保存配置文件"""
        with open(self.config_path, 'w', encoding='utf-8') as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)

    @property
    def podcasts(self) -> List[dict]:
        return self.data.get("podcasts", [])

    @property
    def download_dir(self) -> str:
        return self.data.get("download_dir", "./downloads")

    @property
    def max_workers(self) -> int:
        return self.data.get("max_workers", 3)


class RSSParser:
    """RSS Feed 解析器"""

    ITUNES_NS = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"

    def __init__(self, rss_url: str, headers: dict = None):
        self.rss_url = rss_url
        self.headers = headers or {"User-Agent": "Mozilla/5.0"}

    def fetch(self) -> str:
        """获取 RSS Feed 内容"""
        response = requests.get(self.rss_url, headers=self.headers, timeout=30)
        response.raise_for_status()
        return response.content

    def parse(self) -> tuple:
        """解析 RSS Feed，返回播客信息和节目列表"""
        content = self.fetch()
        root = ET.fromstring(content)
        channel = root.find('channel')

        # 播客信息
        podcast_info = {
            'title': self._get_text(channel, 'title'),
            'author': self._get_text(channel, f'{self.ITUNES_NS}author'),
            'description': self._get_text(channel, 'description'),
            'link': self._get_text(channel, 'link'),
        }

        # 节目列表
        episodes = []
        for item in channel.findall('item'):
            episode = self._parse_item(item)
            if episode:
                episodes.append(episode)

        return podcast_info, episodes

    def _get_text(self, element, tag: str) -> str:
        """获取元素文本"""
        elem = element.find(tag)
        return elem.text if elem is not None else ""

    def _parse_item(self, item) -> Optional[Episode]:
        """解析单个节目"""
        try:
            title = item.find('title').text

            # 获取音频 URL
            enclosure = item.find('enclosure')
            if enclosure is None:
                return None
            audio_url = enclosure.get('url')

            # 获取发布日期
            pub_date_elem = item.find('pubDate')
            pub_date_str = pub_date_elem.text if pub_date_elem is not None else ""
            pub_date = self._parse_date(pub_date_str)

            # 获取时长
            duration_elem = item.find(f'{self.ITUNES_NS}duration')
            duration = duration_elem.text if duration_elem is not None else "0"

            # 获取 GUID
            guid_elem = item.find('guid')
            guid = guid_elem.text if guid_elem is not None else ""

            # 获取描述
            desc_elem = item.find('description')
            description = desc_elem.text[:500] if desc_elem is not None and desc_elem.text else ""

            return Episode(
                title=title,
                audio_url=audio_url,
                pub_date=pub_date,
                duration=duration,
                description=description,
                guid=guid
            )
        except Exception as e:
            print(f"解析节目失败: {e}")
            return None

    def _parse_date(self, date_str: str) -> str:
        """解析日期字符串"""
        if not date_str:
            return ""
        try:
            # RSS 日期格式: Wed, 12 Mar 2026 23:00:00 GMT
            dt = datetime.strptime(date_str[:25], '%a, %d %b %Y %H:%M:%S')
            return dt.strftime('%Y-%m-%d')
        except:
            return date_str[:10] if len(date_str) >= 10 else ""


class DownloadManager:
    """下载管理器"""

    def __init__(self, config: Config):
        self.config = config
        self.headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        self.lock = threading.Lock()
        self.downloaded_count = 0
        self.skipped_count = 0
        self.failed_count = 0

    def sanitize_filename(self, name: str) -> str:
        """清理文件名"""
        name = re.sub(r'[<>:"/\\|?*]', '', name)
        return name[:80]

    def get_audio_extension(self, url: str) -> str:
        """获取音频文件扩展名"""
        if '.m4a' in url.lower():
            return '.m4a'
        elif '.mp3' in url.lower():
            return '.mp3'
        elif '.mp4' in url.lower():
            return '.mp4'
        return '.m4a'

    def get_download_path(self, podcast_name: str, episode: Episode) -> str:
        """获取下载路径"""
        base_dir = Path(self.config.download_dir) / self.sanitize_filename(podcast_name)

        # 日期分类
        date = episode.pub_date if episode.pub_date else "unknown"
        date_dir = date[:7] if len(date) >= 7 else "unknown"

        # 文件名
        safe_title = self.sanitize_filename(episode.title)
        ext = self.get_audio_extension(episode.audio_url)
        filename = f"{date}_{safe_title}{ext}"

        return str(base_dir / date_dir / filename)

    def is_downloaded(self, filepath: str) -> bool:
        """检查文件是否已下载"""
        if not os.path.exists(filepath):
            return False
        # 检查文件大小，避免下载不完整的文件
        size = os.path.getsize(filepath)
        return size > 10000  # 至少 10KB

    def download_episode(self, podcast_name: str, episode: Episode) -> DownloadResult:
        """下载单个节目"""
        filepath = self.get_download_path(podcast_name, episode)

        # 检查是否已下载
        if self.is_downloaded(filepath):
            with self.lock:
                self.skipped_count += 1
            return DownloadResult(
                episode=episode,
                success=True,
                filepath=filepath,
                skipped=True
            )

        # 创建目录
        os.makedirs(os.path.dirname(filepath), exist_ok=True)

        # 下载文件
        retry_times = self.config.data.get('retry_times', 3)
        retry_delay = self.config.data.get('retry_delay', 5)
        timeout = self.config.data.get('download_timeout', 120)

        for attempt in range(retry_times):
            try:
                response = requests.get(
                    episode.audio_url,
                    headers=self.headers,
                    stream=True,
                    timeout=timeout
                )
                response.raise_for_status()

                total_size = int(response.headers.get('content-length', 0))

                with open(filepath, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        f.write(chunk)

                # 验证下载
                if os.path.exists(filepath) and os.path.getsize(filepath) > 10000:
                    with self.lock:
                        self.downloaded_count += 1
                    return DownloadResult(
                        episode=episode,
                        success=True,
                        filepath=filepath
                    )
                else:
                    os.remove(filepath)

            except Exception as e:
                if attempt < retry_times - 1:
                    time.sleep(retry_delay)
                else:
                    with self.lock:
                        self.failed_count += 1
                    return DownloadResult(
                        episode=episode,
                        success=False,
                        error=str(e)
                    )

        return DownloadResult(episode=episode, success=False, error="Unknown error")

    def download_episodes(self, podcast_name: str, episodes: List[Episode],
                          max_workers: int = 3, callback=None) -> List[DownloadResult]:
        """批量下载节目"""
        results = []
        total = len(episodes)

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(self.download_episode, podcast_name, ep): ep
                for ep in episodes
            }

            for i, future in enumerate(as_completed(futures), 1):
                result = future.result()
                results.append(result)

                if callback:
                    callback(i, total, result)

        return results


class XiaoyuzhouDownloader:
    """小宇宙播客下载器主类"""

    def __init__(self, config_path: str = "config.json"):
        self.config = Config(config_path)
        self.download_manager = DownloadManager(self.config)

    def get_rss_url(self, podcast_id: str) -> str:
        """根据播客 ID 生成 RSS URL"""
        # 尝试喜马拉雅 RSS
        return f"https://www.ximalaya.com/album/{podcast_id}.xml"

    def get_podcast_id_from_url(self, url: str) -> str:
        """从 URL 提取播客 ID"""
        # 小宇宙 URL 格式: https://www.xiaoyuzhoufm.com/podcast/{id}
        match = re.search(r'podcast/([a-f0-9]+)', url)
        if match:
            return match.group(1)
        return url

    def find_rss_feed(self, podcast_name: str) -> Optional[str]:
        """通过 Apple Podcasts 查找 RSS feed"""
        try:
            search_url = f"https://itunes.apple.com/search?media=podcast&entity=podcast&term={requests.utils.quote(podcast_name)}&limit=5"
            response = requests.get(search_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
            data = response.json()

            for item in data.get('results', []):
                if podcast_name in item.get('collectionName', ''):
                    return item.get('feedUrl')
        except:
            pass
        return None

    def load_episodes_cache(self, podcast_name: str) -> Dict:
        """加载节目缓存"""
        cache_path = Path(self.config.download_dir) / self.download_manager.sanitize_filename(podcast_name) / "episodes_cache.json"
        if cache_path.exists():
            with open(cache_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        return {"episodes": [], "last_update": ""}

    def save_episodes_cache(self, podcast_name: str, episodes: List[Episode]):
        """保存节目缓存"""
        cache_path = Path(self.config.download_dir) / self.download_manager.sanitize_filename(podcast_name) / "episodes_cache.json"
        cache_path.parent.mkdir(parents=True, exist_ok=True)

        cache_data = {
            "episodes": [asdict(ep) for ep in episodes],
            "last_update": datetime.now().isoformat(),
            "total_count": len(episodes)
        }

        with open(cache_path, 'w', encoding='utf-8') as f:
            json.dump(cache_data, f, ensure_ascii=False, indent=2)

    def check_updates(self, podcast_config: dict) -> List[Episode]:
        """检查更新，返回新节目列表"""
        podcast_name = podcast_config.get('name', '')
        rss_url = podcast_config.get('rss_url')

        if not rss_url:
            print(f"  未配置 RSS URL，尝试自动查找...")
            rss_url = self.find_rss_feed(podcast_name)

        if not rss_url:
            print(f"  无法找到 RSS feed")
            return []

        # 解析 RSS
        print(f"  正在获取节目列表...")
        parser = RSSParser(rss_url)
        podcast_info, episodes = parser.parse()

        # 加载缓存
        cache = self.load_episodes_cache(podcast_name)
        cached_guids = {ep.get('guid') for ep in cache.get('episodes', [])}

        # 找出新节目
        new_episodes = [ep for ep in episodes if ep.guid and ep.guid not in cached_guids]

        return new_episodes, episodes, podcast_info

    def download_podcast(self, podcast_config: dict, only_new: bool = False):
        """下载播客"""
        podcast_name = podcast_config.get('name', 'Unknown Podcast')
        print(f"\n{'='*60}")
        print(f"播客: {podcast_name}")
        print(f"{'='*60}")

        # 检查更新
        new_episodes, all_episodes, podcast_info = self.check_updates(podcast_config)

        print(f"  总节目数: {len(all_episodes)}")
        print(f"  新节目数: {len(new_episodes)}")

        # 更新播客名称
        if podcast_info.get('title'):
            podcast_name = podcast_info['title']

        # 确定要下载的节目
        if only_new:
            episodes_to_download = new_episodes
        else:
            episodes_to_download = all_episodes

        if not episodes_to_download:
            print("  没有需要下载的节目")
            return

        print(f"\n开始下载 {len(episodes_to_download)} 个节目...")

        # 下载进度回调
        def progress_callback(current, total, result):
            status = "已存在" if result.skipped else ("成功" if result.success else "失败")
            title = result.episode.title[:40]
            print(f"[{current}/{total}] {status}: {title}...")

        # 执行下载
        self.download_manager.download_episodes(
            podcast_name,
            episodes_to_download,
            max_workers=self.config.max_workers,
            callback=progress_callback
        )

        # 保存缓存
        self.save_episodes_cache(podcast_name, all_episodes)

        # 打印统计
        print(f"\n下载完成:")
        print(f"  新下载: {self.download_manager.downloaded_count}")
        print(f"  已存在: {self.download_manager.skipped_count}")
        print(f"  失败: {self.download_manager.failed_count}")

    def add_podcast(self, name: str, rss_url: str = None, xiaoyuzhou_url: str = None):
        """添加播客"""
        podcast = {"name": name}

        if xiaoyuzhou_url:
            podcast['xiaoyuzhou_url'] = xiaoyuzhou_url

        if rss_url:
            podcast['rss_url'] = rss_url

        self.config.data['podcasts'].append(podcast)
        self.config.save()
        print(f"已添加播客: {name}")

    def list_podcasts(self):
        """列出所有播客"""
        print("\n已配置的播客:")
        print("-" * 40)
        for i, podcast in enumerate(self.config.podcasts, 1):
            name = podcast.get('name', 'Unknown')
            rss = podcast.get('rss_url', '未配置')
            print(f"{i}. {name}")
            print(f"   RSS: {rss[:50]}..." if len(rss) > 50 else f"   RSS: {rss}")
        print("-" * 40)

    def run(self, only_new: bool = False, podcast_names: List[str] = None):
        """运行下载器"""
        podcasts = self.config.podcasts

        if podcast_names:
            podcasts = [p for p in podcasts if p.get('name') in podcast_names]

        if not podcasts:
            print("没有配置任何播客，请先添加播客")
            print("使用 --add 命令添加播客")
            return

        for podcast in podcasts:
            try:
                self.download_podcast(podcast, only_new)
            except Exception as e:
                print(f"下载失败: {e}")


def create_default_config():
    """创建默认配置文件"""
    default_config = {
        "podcasts": [
            {
                "name": "声动早咖啡",
                "rss_url": "https://www.ximalaya.com/album/51076156.xml",
                "xiaoyuzhou_url": "https://www.xiaoyuzhoufm.com/podcast/60de7c003dd577b40d5a40f3"
            }
        ],
        "download_dir": "./downloads",
        "max_workers": 3,
        "retry_times": 3,
        "retry_delay": 5,
        "check_updates": True
    }

    with open("config.json", 'w', encoding='utf-8') as f:
        json.dump(default_config, f, ensure_ascii=False, indent=2)

    print("已创建默认配置文件: config.json")


def main():
    parser = argparse.ArgumentParser(
        description="小宇宙播客下载器 - 下载小宇宙播客的所有节目",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s                      # 下载所有已配置的播客
  %(prog)s --only-new           # 只下载新节目
  %(prog)s --list               # 列出所有已配置的播客
  %(prog)s --add "播客名称" --rss "RSS URL"  # 添加新播客
  %(prog)s --init               # 创建默认配置文件
        """
    )

    parser.add_argument('--init', action='store_true', help='创建默认配置文件')
    parser.add_argument('--list', action='store_true', help='列出所有已配置的播客')
    parser.add_argument('--only-new', action='store_true', help='只下载新节目')
    parser.add_argument('--add', metavar='NAME', help='添加播客')
    parser.add_argument('--rss', metavar='URL', help='RSS feed URL')
    parser.add_argument('--url', metavar='URL', help='小宇宙播客 URL')
    parser.add_argument('--config', metavar='PATH', default='config.json', help='配置文件路径')
    parser.add_argument('--download-dir', metavar='DIR', help='下载目录')
    parser.add_argument('--workers', type=int, metavar='N', help='并发下载线程数')

    args = parser.parse_args()

    # 创建默认配置
    if args.init:
        create_default_config()
        return

    # 检查配置文件是否存在
    if not os.path.exists(args.config):
        print(f"配置文件不存在: {args.config}")
        print("正在创建默认配置文件...")
        create_default_config()

    # 初始化下载器
    downloader = XiaoyuzhouDownloader(args.config)

    # 覆盖配置
    if args.download_dir:
        downloader.config.data['download_dir'] = args.download_dir
    if args.workers:
        downloader.config.data['max_workers'] = args.workers

    # 执行命令
    if args.list:
        downloader.list_podcasts()
    elif args.add:
        downloader.add_podcast(args.add, args.rss, args.url)
    else:
        downloader.run(only_new=args.only_new)


if __name__ == "__main__":
    main()