# db.py

import sqlite3
from datetime import datetime
import threading
from typing import Dict, List, Tuple, Any, Optional
from config import DB_PATH
from logger import setup_logger

logger = setup_logger("database")

class Database:
    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path
        self.conn = None
        self.lock = threading.Lock()  # 添加线程锁
        self._connect()
        self._init_tables()

    def _connect(self):
        """建立数据库连接并启用WAL模式"""
        try:
            self.conn = sqlite3.connect(
                self.db_path,
                check_same_thread=False,
                timeout=30  # 设置较长超时时间
            )
            self.conn.execute('PRAGMA journal_mode=WAL;')  # 启用WAL提升并发性能
            self.conn.execute('PRAGMA busy_timeout=5000')  # 设置等待锁的最长时间
            logger.info(f"數據庫連接成功: {self.db_path}")
        except Exception as e:
            logger.error(f"數據庫連接失敗: {e}")
            raise

    def _init_tables(self):
        """初始化数据库表结构"""
        try:
            cursor = self.conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS completed_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id TEXT,
                    symbol TEXT,
                    side TEXT,
                    quantity REAL,
                    price REAL,
                    maker BOOLEAN,
                    fee REAL,
                    fee_asset TEXT,
                    trade_type TEXT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_completed_orders_symbol ON completed_orders(symbol)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_completed_orders_timestamp ON completed_orders(timestamp)")

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS trading_stats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT,
                    symbol TEXT,
                    maker_buy_volume REAL DEFAULT 0,
                    maker_sell_volume REAL DEFAULT 0,
                    taker_buy_volume REAL DEFAULT 0,
                    taker_sell_volume REAL DEFAULT 0,
                    realized_profit REAL DEFAULT 0,
                    total_fees REAL DEFAULT 0,
                    net_profit REAL DEFAULT 0,
                    avg_spread REAL DEFAULT 0,
                    trade_count INTEGER DEFAULT 0,
                    volatility REAL DEFAULT 0
                )
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_trading_stats_date ON trading_stats(date)")

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS rebalance_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id TEXT,
                    symbol TEXT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS market_data (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT,
                    price REAL,
                    volume REAL,
                    bid_ask_spread REAL,
                    liquidity_score REAL,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            self.conn.commit()
            logger.info("數據庫表結構已初始化")
        except Exception as e:
            logger.error(f"初始化資料庫表時出錯: {e}")
            self.conn.rollback()
            raise
        finally:
            cursor.close()

    def execute(self, query: str, params: Optional[tuple] = None) -> sqlite3.Cursor:
        """执行SQL查询（带锁）"""
        with self.lock:
            cursor = self.conn.cursor()
            try:
                if params:
                    cursor.execute(query, params)
                else:
                    cursor.execute(query)
                self.conn.commit()
                return cursor
            except sqlite3.OperationalError as e:
                logger.warning(f"SQL執行錯誤: {e}, 查詢: {query}")
                self.conn.rollback()
                cursor.close()
                raise

    def executemany(self, query: str, params_list: List[tuple]) -> sqlite3.Cursor:
        """批量执行 SQL 查询"""
        with self.lock:
            cursor = self.conn.cursor()
            try:
                cursor.executemany(query, params_list)
                self.conn.commit()
                return cursor
            except sqlite3.OperationalError as e:
                logger.warning(f"批量SQL執行錯誤: {e}, 查詢: {query}")
                self.conn.rollback()
                cursor.close()
                raise

    def commit(self):
        """提交事务"""
        with self.lock:
            try:
                self.conn.commit()
            except sqlite3.OperationalError as e:
                logger.debug(f"提交事務時發生操作錯誤: {e}")

    def rollback(self):
        """回滚事务"""
        with self.lock:
            try:
                self.conn.rollback()
            except sqlite3.OperationalError as e:
                logger.debug(f"回滾事務時發生操作錯誤: {e}")

    def close(self):
        """关闭数据库连接"""
        with self.lock:
            if self.conn:
                self.conn.close()
                logger.info("數據庫連接已關閉")

    def insert_order(self, order_data: dict) -> Optional[int]:
        """
        插入订单记录

        Args:
            order_data: 订单数据字典

        Returns:
            插入的行ID
        """
        with self.lock:
            try:
                query = """
                    INSERT INTO completed_orders 
                    (order_id, symbol, side, quantity, price, maker, fee, fee_asset, trade_type)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """
                params = (
                    order_data.get('order_id'),
                    order_data.get('symbol'),
                    order_data.get('side'),
                    order_data.get('quantity'),
                    order_data.get('price'),
                    1 if order_data.get('maker') else 0,
                    order_data.get('fee', 0),
                    order_data.get('fee_asset', ''),
                    order_data.get('trade_type', 'manual')
                )

                cursor = self.conn.cursor()
                cursor.execute(query, params)
                self.conn.commit()
                return cursor.lastrowid
            except Exception as e:
                logger.error(f"插入訂單記錄時出錯: {e}")
                self.conn.rollback()
                return None
            finally:
                cursor.close()

    def update_trading_stats(self, stats_data: dict) -> bool:
        """
        更新交易统计信息

        Args:
            stats_data: 统计数据字典

        Returns:
            布尔值表示更新是否成功
        """
        with self.lock:
            try:
                cursor = self.conn.cursor()

                # 检查今日记录是否存在
                check_query = "SELECT id FROM trading_stats WHERE date = ? AND symbol = ?"
                cursor.execute(check_query, (stats_data['date'], stats_data['symbol']))
                record = cursor.fetchone()

                if record:
                    # 更新现有记录
                    update_query = """
                        UPDATE trading_stats SET
                            maker_buy_volume = ?,
                            maker_sell_volume = ?,
                            taker_buy_volume = ?,
                            taker_sell_volume = ?,
                            realized_profit = ?,
                            total_fees = ?,
                            net_profit = ?,
                            avg_spread = ?,
                            trade_count = ?,
                            volatility = ?
                        WHERE date = ? AND symbol = ?
                    """
                    params = (
                        stats_data['maker_buy_volume'],
                        stats_data['maker_sell_volume'],
                        stats_data['taker_buy_volume'],
                        stats_data['taker_sell_volume'],
                        stats_data['realized_profit'],
                        stats_data['total_fees'],
                        stats_data['net_profit'],
                        stats_data['avg_spread'],
                        stats_data['trade_count'],
                        stats_data['volatility'],
                        stats_data['date'],
                        stats_data['symbol']
                    )
                    cursor.execute(update_query, params)
                else:
                    # 插入新记录
                    insert_query = """
                        INSERT INTO trading_stats
                        (date, symbol, maker_buy_volume, maker_sell_volume, taker_buy_volume, taker_sell_volume,
                         realized_profit, total_fees, net_profit, avg_spread, trade_count, volatility)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """
                    params = (
                        stats_data['date'],
                        stats_data['symbol'],
                        stats_data['maker_buy_volume'],
                        stats_data['maker_sell_volume'],
                        stats_data['taker_buy_volume'],
                        stats_data['taker_sell_volume'],
                        stats_data['realized_profit'],
                        stats_data['total_fees'],
                        stats_data['net_profit'],
                        stats_data['avg_spread'],
                        stats_data['trade_count'],
                        stats_data['volatility']
                    )
                    cursor.execute(insert_query, params)

                self.conn.commit()
                return True
            except Exception as e:
                logger.error(f"更新交易統計時出錯: {e}")
                self.conn.rollback()
                return False
            finally:
                cursor.close()

    def get_trading_stats(self, symbol: str, date: Optional[str] = None) -> List[dict]:
        """
        获取交易统计数据

        Args:
            symbol: 交易对符号
            date: 日期字符串，如果为None，则获取所有日期的统计

        Returns:
            统计数据列表
        """
        with self.lock:
            cursor = self.conn.cursor()
            try:
                if date:
                    query = "SELECT * FROM trading_stats WHERE symbol = ? AND date = ? ORDER BY date DESC"
                    cursor.execute(query, (symbol, date))
                else:
                    query = "SELECT * FROM trading_stats WHERE symbol = ? ORDER BY date DESC"
                    cursor.execute(query, (symbol,))

                columns = [desc[0] for desc in cursor.description]
                result = [dict(zip(columns, row)) for row in cursor.fetchall()]
                return result
            except Exception as e:
                logger.error(f"获取交易统计失败: {e}")
                return []
            finally:
                cursor.close()

    def get_recent_trades(self, symbol: str, limit: int = 10) -> List[Dict[str, Any]]:
        """
        获取最近成交记录

        Args:
            symbol: 交易对符号
            limit: 返回记录数量限制

        Returns:
            成交记录列表
        """
        with self.lock:
            cursor = self.conn.cursor()
            try:
                query = """
                    SELECT side, quantity, price, maker, fee, timestamp
                    FROM completed_orders
                    WHERE symbol = ?
                    ORDER BY timestamp DESC
                    LIMIT ?
                """
                cursor.execute(query, (symbol, limit))
                columns = ['side', 'quantity', 'price', 'maker', 'fee', 'timestamp']
                result = [dict(zip(columns, row)) for row in cursor.fetchall()]
                return result
            except Exception as e:
                logger.error(f"获取历史成交记录失败: {e}")
                return []
            finally:
                cursor.close()

    def get_order_history(self, symbol: str, limit: int = 1000) -> List[Tuple]:
        """
        获取订单历史

        Args:
            symbol: 交易对符号
            limit: 返回记录数量限制

        Returns:
            订单历史列表
        """
        with self.lock:
            if not isinstance(symbol, str) or not symbol.strip():
                raise ValueError("Symbol 必须是有效字符串")
            if not isinstance(limit, int) or limit <= 0:
                raise ValueError("Limit 必须是正整数")

            cursor = self.conn.cursor()
            try:
                query = """
                    SELECT side, quantity, price, maker, fee
                    FROM completed_orders
                    WHERE symbol = ?
                    ORDER BY timestamp DESC
                    LIMIT ?
                """
                cursor.execute(query, (symbol, limit))
                result = cursor.fetchall()
                return result
            except Exception as e:
                logger.error(f"获取订单历史失败: {e}")
                return []
            finally:
                cursor.close()

    def record_rebalance_order(self, order_id: str, symbol: str) -> Optional[int]:
        """记录重平衡订单"""
        with self.lock:
            try:
                cursor = self.conn.cursor()
                query = "INSERT INTO rebalance_orders (order_id, symbol) VALUES (?, ?)"
                cursor.execute(query, (order_id, symbol))
                self.conn.commit()
                return cursor.lastrowid
            except Exception as e:
                logger.error(f"记录重平衡订单时出错: {e}")
                self.conn.rollback()
                return None
            finally:
                cursor.close()

    def is_rebalance_order(self, order_id: str, symbol: str) -> bool:
        """检查订单是否为重平衡订单"""
        with self.lock:
            cursor = self.conn.cursor()
            try:
                query = "SELECT id FROM rebalance_orders WHERE order_id = ? AND symbol = ?"
                cursor.execute(query, (order_id, symbol))
                return cursor.fetchone() is not None
            except Exception as e:
                logger.error(f"检查重平衡订单时出错: {e}")
                return False
            finally:
                cursor.close()

    def update_market_data(self, market_data: dict) -> Optional[int]:
        """更新市场数据"""
        with self.lock:
            try:
                query = """
                    INSERT INTO market_data 
                    (symbol, price, volume, bid_ask_spread, liquidity_score)
                    VALUES (?, ?, ?, ?, ?)
                """
                params = (
                    market_data['symbol'],
                    market_data['price'],
                    market_data['volume'],
                    market_data['bid_ask_spread'],
                    market_data['liquidity_score']
                )
                cursor = self.conn.cursor()
                cursor.execute(query, params)
                self.conn.commit()
                return cursor.lastrowid
            except Exception as e:
                logger.error(f"更新市場數據時出錯: {e}")
                self.conn.rollback()
                return None
            finally:
                cursor.close()

    def get_all_time_stats(self, symbol: str) -> Optional[Dict[str, float]]:
        """获取所有时间的总计统计"""
        with self.lock:
            cursor = self.conn.cursor()
            try:
                query = """
                    SELECT 
                        SUM(maker_buy_volume), SUM(maker_sell_volume),
                        SUM(taker_buy_volume), SUM(taker_sell_volume),
                        SUM(realized_profit), SUM(total_fees),
                        SUM(net_profit), AVG(avg_spread)
                    FROM trading_stats
                    WHERE symbol = ?
                """
                cursor.execute(query, (symbol,))
                result = cursor.fetchone()

                if result and result[0] is not None:
                    keys = [
                        'total_maker_buy',
                        'total_maker_sell',
                        'total_taker_buy',
                        'total_taker_sell',
                        'total_profit',
                        'total_fees',
                        'total_net_profit',
                        'avg_spread_all_time'
                    ]
                    return dict(zip(keys, result))
                return None
            except Exception as e:
                logger.error(f"获取累计统计失败: {e}")
                return None
            finally:
                cursor.close()
