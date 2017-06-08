import logging
from datetime import timedelta

import arrow
from sqlalchemy import and_
from telegram.error import NetworkError
from telegram.ext import CommandHandler, Updater
from telegram import ParseMode, Bot, Update
from wrapt import synchronized

from persistence import Trade, Session
from exchange import get_exchange_api
from utils import get_conf

# Remove noisy log messages
logging.getLogger('requests.packages.urllib3').setLevel(logging.INFO)
logging.getLogger('telegram').setLevel(logging.INFO)
logger = logging.getLogger(__name__)

_updater = None

conf = get_conf()
api_wrapper = get_exchange_api(conf)


def authorized_only(command_handler):
    """
    Decorator to check if the message comes from the correct chat_id
    :param command_handler: Telegram CommandHandler
    :return: decorated function
    """
    def wrapper(*args, **kwargs):
        bot, update = args[0], args[1]
        if not isinstance(bot, Bot) or not isinstance(update, Update):
            raise ValueError('Received invalid Arguments: {}'.format(*args))

        chat_id = int(conf['telegram']['chat_id'])
        if int(update.message.chat_id) == chat_id:
            logger.info('Executing handler: {} for chat_id: {}'.format(command_handler.__name__, chat_id))
            return command_handler(*args, **kwargs)
        else:
            logger.info('Rejected unauthorized message from: {}'.format(update.message.chat_id))
    return wrapper


class TelegramHandler(object):
    @staticmethod
    @authorized_only
    def _status(bot, update):
        """
        Handler for /status.
        Returns the current TradeThread status
        :param bot: telegram bot
        :param update: message update
        :return: None
        """
        # Fetch open trade
        trades = Trade.query.filter(Trade.is_open.is_(True)).all()
        from main import get_instance
        if not get_instance().is_alive():
            TelegramHandler.send_msg('*Status:* `trader is not running`', bot=bot)
        elif not trades:
            TelegramHandler.send_msg('*Status:* `no active order`', bot=bot)
        else:
            for trade in trades:
                # calculate profit and send message to user
                current_rate = api_wrapper.get_ticker(trade.pair)['bid']
                current_profit = 100 * ((current_rate - trade.open_rate) / trade.open_rate)
                orders = api_wrapper.get_open_orders(trade.pair)
                orders = [o for o in orders if o['id'] == trade.open_order_id]
                order = orders[0] if orders else None
                message = """
*Trade ID:* `{trade_id}`
*Current Pair:* [{pair}]({market_url})
*Open Since:* `{date}`
*Amount:* `{amount}`
*Open Rate:* `{open_rate}`
*Close Rate:* `{close_rate}`
*Current Rate:* `{current_rate}`
*Close Profit:* `{close_profit}`
*Current Profit:* `{current_profit}%`
*Open Order:* `{open_order}`
                        """.format(
                    trade_id=trade.id,
                    pair=trade.pair,
                    market_url=api_wrapper.get_pair_detail_url(trade.pair),
                    date=arrow.get(trade.open_date).humanize(),
                    open_rate=trade.open_rate,
                    close_rate=trade.close_rate,
                    current_rate=current_rate,
                    amount=round(trade.amount, 8),
                    close_profit='{}%'.format(round(trade.close_profit, 2)) if trade.close_profit else None,
                    current_profit=round(current_profit, 2),
                    open_order='{} ({})'.format(order['remaining'], order['type']) if order else None,
                )
                TelegramHandler.send_msg(message, bot=bot)

    @staticmethod
    @authorized_only
    def _profit(bot, update):
        """
        Handler for /profit.
        Returns a cumulative profit statistics.
        :param bot: telegram bot
        :param update: message update
        :return: None
        """
        trades = Trade.query.filter(Trade.is_open.is_(False)).all()
        trade_count = len(trades)
        profit_amount = sum((t.close_profit / 100) * t.btc_amount for t in trades)
        profit = sum(t.close_profit for t in trades)
        if trades:
            avg_stake_amount = round(sum(t.btc_amount for t in trades) / float(trade_count), 8)
        else:
            avg_stake_amount = None
        durations_hours = [(t.close_date - t.open_date).total_seconds() / 3600.0 for t in trades]
        avg_duration = sum(durations_hours) / float(len(durations_hours))
        markdown_msg = """
*ROI:* `{profit_btc} ({profit}%)`
*Trade Count:* `{trade_count}`
*First Trade completed:* `{first_trade_date}`
*Latest Trade completed:* `{latest_trade_date}`
*Avg. Stake Amount:* `{avg_stake_amount}`
*Avg. Duration:* `{avg_duration}` 
    """.format(
            profit_btc=round(profit_amount, 8),
            profit=round(profit, 2),
            trade_count=trade_count,
            first_trade_date=arrow.get(trades[0].open_date).humanize(),
            latest_trade_date=arrow.get(trades[-1].open_date).humanize(),
            avg_stake_amount=avg_stake_amount,
            avg_duration=str(timedelta(hours=avg_duration)).split('.')[0],
        )
        TelegramHandler.send_msg(markdown_msg, bot=bot)

    @staticmethod
    @authorized_only
    def _start(bot, update):
        """
        Handler for /start.
        Starts TradeThread
        :param bot: telegram bot
        :param update: message update
        :return: None
        """
        from main import get_instance
        if get_instance().is_alive():
            TelegramHandler.send_msg('*Status:* `already running`', bot=bot)
        else:
            get_instance(recreate=True).start()

    @staticmethod
    @authorized_only
    def _stop(bot, update):
        """
        Handler for /stop.
        Stops TradeThread
        :param bot: telegram bot
        :param update: message update
        :return: None
        """
        from main import get_instance, stop_instance
        if get_instance().is_alive():
            TelegramHandler.send_msg('`Stopping trader ...`', bot=bot)
            stop_instance()
        else:
            TelegramHandler.send_msg('*Status:* `already stopped`', bot=bot)

    @staticmethod
    @authorized_only
    def _forcesell(bot, update):
        """
        Handler for /forcesell <id>.
        Sells the given trade at current price
        :param bot: telegram bot
        :param update: message update
        :return: None
        """
        from main import get_instance
        if not get_instance().is_alive():
            TelegramHandler.send_msg('`trader is not running`', bot=bot)
            return

        try:
            trade_id = int(update.message.text
                           .replace('/forcesell', '')
                           .strip())
            # Query for trade
            trade = Trade.query.filter(and_(
                Trade.id == trade_id,
                Trade.is_open.is_(True)
            )).first()
            if not trade:
                TelegramHandler.send_msg('There is no open trade with ID: `{}`'.format(trade_id))
                return
            # Get current rate
            current_rate = api_wrapper.get_ticker(trade.pair)['bid']
            # Get available balance
            currency = trade.pair.split('_')[1]
            balance = api_wrapper.get_balance(currency)
            # Execute sell
            profit = trade.exec_sell_order(current_rate, balance)
            message = '*{}:* Selling [{}]({}) at rate `{:f} (profit: {}%)`'.format(
                trade.exchange.name,
                trade.pair.replace('_', '/'),
                api_wrapper.get_pair_detail_url(trade.pair),
                trade.close_rate,
                round(profit, 2)
            )
            logger.info(message)
            TelegramHandler.send_msg(message)

        except ValueError:
            TelegramHandler.send_msg('Invalid argument. Usage: `/forcesell <trade_id>`')
            logger.warning('/forcesell: Invalid argument received')

    @staticmethod
    @synchronized
    def get_updater(conf):
        """
        Returns the current telegram updater instantiates a new one
        :param conf:
        :return: telegram.ext.Updater
        """
        global _updater
        if not _updater:
            _updater = Updater(token=conf['telegram']['token'], workers=0)
        return _updater

    @staticmethod
    def listen():
        """
        Registers all known command handlers and starts polling for message updates
        :return: None
        """
        # Register command handler and start telegram message polling
        handles = [
            CommandHandler('status', TelegramHandler._status),
            CommandHandler('profit', TelegramHandler._profit),
            CommandHandler('start', TelegramHandler._start),
            CommandHandler('stop', TelegramHandler._stop),
            CommandHandler('forcesell', TelegramHandler._forcesell),
        ]
        for handle in handles:
            TelegramHandler.get_updater(conf).dispatcher.add_handler(handle)
        TelegramHandler.get_updater(conf).start_polling(clean=True, bootstrap_retries=3)
        logger.info('TelegramHandler is listening for following commands: {}'
                    .format([h.command for h in handles]))

    @staticmethod
    def send_msg(msg, bot=None, parse_mode=ParseMode.MARKDOWN):
        """
        Send given markdown message
        :param msg: message
        :param bot: alternative bot
        :param parse_mode: telegram parse mode
        :return: None
        """
        if conf['telegram'].get('enabled', False):
            try:
                bot = bot or TelegramHandler.get_updater(conf).bot
                try:
                    bot.send_message(conf['telegram']['chat_id'], msg, parse_mode=parse_mode)
                except NetworkError as e:
                    # Sometimes the telegram server resets the current connection,
                    # if this is the case we send the message again.
                    logger.warning('Got Telegram NetworkError: {}! trying one more time'.format(e.message))
                    bot.send_message(conf['telegram']['chat_id'], msg, parse_mode=parse_mode)
            except Exception:
                logger.exception('Exception occurred within Telegram API')
