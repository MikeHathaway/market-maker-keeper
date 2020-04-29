# This file is part of Maker Keeper Framework.
#
# Copyright (C) 2020 MikeHathaway
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import argparse
import logging
import sys
import time
import asyncio
from functools import partial

import threading

from pyexchange.erisx import ErisxApi
from pyexchange.model import Order

from pymaker.numeric import Wad

# from lib.pyexchange.pyexchange.erisx import ErisxApi
from market_maker_keeper.cex_api import CEXKeeperAPI
from market_maker_keeper.order_history_reporter import create_order_history_reporter
from market_maker_keeper.limit import History
from market_maker_keeper.order_book import OrderBookManager, OrderBook


# Subclass orderboook to enable support for different order schema required by ErisX
class ErisXOrderBookManager(OrderBookManager):

    # place orders sequentially instead of spinning up seperate threads
    # Due to nature of FIX engine, there is a single socket connection controlled by a single event loop.
    # ThreadExecutor collides with eachother
    def place_order(self, place_order_function):
        """Places new order. Order placement will happen in a background thread.

        Args:
            place_order_function: Function used to place the order.
        """
        assert(callable(place_order_function))

        with self._lock:
            self._currently_placing_orders += 1
        # self._lock.acquire()
        # self._currently_placing_orders += 1
        self._report_order_book_updated()
        try:
            with self._lock:
            # with self._lock.acquire(True, 10):
                new_order = place_order_function()

                if new_order is not None:
                    # with self._lock:
                    self._orders_placed.append(new_order)
        except BaseException as exception:
            self.logger.exception(exception)
        finally:
            with self._lock:
                self._currently_placing_orders -= 1
            self._report_order_book_updated()

            # self._lock.release()

    def cancel_orders(self, orders: list):
        """Cancels existing orders. Order cancellation will happen in a background thread.

        Args:
            orders: List of orders to cancel.
        """
        assert(isinstance(orders, list))
        assert(callable(self.cancel_order_function))

        with self._lock:
            for order in orders:
                self._order_ids_cancelling.add(order.order_id)

        self._report_order_book_updated()

        for order in orders:
            order_id = order.order_id
            try:
                # with self._lock:
                # with self._lock.acquire(True, 10):
                    if self.cancel_order_function(order):
                        with self._lock:
                            self._order_ids_cancelled.add(order_id)
                            self._order_ids_cancelling.remove(order_id)

                    # self._lock.release()
            except BaseException as exception:
                self.logger.exception(f"Failed to cancel {order_id}")
            finally:
                with self._lock:
                    try:
                        self._order_ids_cancelling.remove(order_id)
                    except KeyError:
                        self.logger.info(f"Failed to remove {order_id}")
                        pass
                self._report_order_book_updated()

class ErisXMarketMakerKeeper(CEXKeeperAPI):
    """
    Keeper acting as a market maker on ErisX.
    Although portions of ErisX are onchain, 
    full order book functionality requires offchain components.
    """

    logger = logging.getLogger()

    def __init__(self, args: list):
        parser = argparse.ArgumentParser(prog='erisx-market-maker-keeper')

        parser.add_argument("--erisx-clearing-url", type=str, required=True,
                            help="Address of the ErisX clearing server")

        parser.add_argument("--fix-trading-endpoint", type=str, required=True,
                            help="FIX endpoint for ErisX trading")

        parser.add_argument("--fix-trading-user", type=str, required=True,
                            help="Account ID for ErisX trading")

        parser.add_argument("--fix-marketdata_endpoint", type=str, required=True,
                            help="FIX endpoint for ErisX market data")

        parser.add_argument("--fix-marketdata_user", type=str, required=True,
                            help="Account ID for ErisX market data")

        parser.add_argument("--erisx-password", type=str, required=True,
                            help="password for FIX account")

        parser.add_argument("--erisx-api-key", type=str, required=True,
                            help="API key for ErisX REST API")

        parser.add_argument("--erisx-api-secret", type=str, required=True,
                            help="API secret for ErisX REST API")                                                        

        parser.add_argument("--pair", type=str, required=True,
                            help="Token pair (sell/buy) on which the keeper will operate")

        parser.add_argument("--config", type=str, required=True,
                            help="Bands configuration file")

        parser.add_argument("--price-feed", type=str, required=True,
                            help="Source of price feed")

        parser.add_argument("--price-feed-expiry", type=int, default=120,
                            help="Maximum age of the price feed (in seconds, default: 120)")

        parser.add_argument("--spread-feed", type=str,
                            help="Source of spread feed")

        parser.add_argument("--spread-feed-expiry", type=int, default=3600,
                            help="Maximum age of the spread feed (in seconds, default: 3600)")

        parser.add_argument("--control-feed", type=str,
                            help="Source of control feed")

        parser.add_argument("--control-feed-expiry", type=int, default=86400,
                            help="Maximum age of the control feed (in seconds, default: 86400)")

        parser.add_argument("--order-history", type=str,
                            help="Endpoint to report active orders to")

        parser.add_argument("--order-history-every", type=int, default=30,
                            help="Frequency of reporting active orders (in seconds, default: 30)")

        parser.add_argument("--refresh-frequency", type=int, default=3,
                            help="Order book refresh frequency (in seconds, default: 3)")

        parser.add_argument("--debug", dest='debug', action='store_true',
                            help="Enable debug output")

        self.arguments = parser.parse_args(args)

        self.erisx_api = ErisxApi(fix_trading_endpoint=self.arguments.fix_trading_endpoint, fix_trading_user=self.arguments.fix_trading_user,
                             fix_marketdata_endpoint=self.arguments.fix_marketdata_endpoint, fix_marketdata_user=self.arguments.fix_marketdata_user,
                             password=self.arguments.erisx_password,
                             clearing_url=self.arguments.erisx_clearing_url,
                             api_key=self.arguments.erisx_api_key, api_secret=self.arguments.erisx_api_secret)

        super().__init__(self.arguments, self.erisx_api)

    def init_order_book_manager(self, arguments, erisx_api):
        self.order_book_manager = ErisXOrderBookManager(refresh_frequency=self.arguments.refresh_frequency)
        # self.order_book_manager = OrderBookManager(refresh_frequency=self.arguments.refresh_frequency)
        self.order_book_manager.get_orders_with(lambda: self.erisx_api.get_orders(self.pair()))
        self.order_book_manager.get_balances_with(lambda: self.erisx_api.get_balances())
        self.order_book_manager.cancel_orders_with(lambda order: self.erisx_api.cancel_order(order.order_id, self.pair(), order.is_sell))
        self.order_book_manager.enable_history_reporting(self.order_history_reporter, self.our_buy_orders,
                                                         self.our_sell_orders)

        self.order_book_manager.pair = self.pair()
        self.order_book_manager.start()

    def pair(self):
        return self.arguments.pair

    def token_sell(self) -> str:
        return self.arguments.pair.split('/')[0].upper()

    def token_buy(self) -> str:
        return self.arguments.pair.split('/')[1].upper()

    def our_available_balance(self, our_balances: dict, token: str) -> Wad:
        if token == 'ETH':
            token = 'TETH'

        token_balances = list(filter(lambda asset: asset['asset_type'].upper() == token, our_balances))
        if token_balances:
            return Wad.from_number(float(token_balances[0]['available_to_trade']))
        else:
            return Wad(0)

    def place_orders(self, new_orders):
        def place_order_function(new_order_to_be_placed):
            amount = new_order_to_be_placed.pay_amount if new_order_to_be_placed.is_sell else new_order_to_be_placed.buy_amount
            # TODO: dynamically determine the allowed decimals
            order_qty_precision = 1

            order_id = self.erisx_api.place_order(pair=self.pair().upper(),
                                                 is_sell=new_order_to_be_placed.is_sell,
                                                 price=round(Wad.__float__(new_order_to_be_placed.price), 18),
                                                 amount=round(Wad.__float__(amount), order_qty_precision))

            return Order(str(order_id), int(time.time()), self.pair(), new_order_to_be_placed.is_sell, new_order_to_be_placed.price, amount)

        # self._async_order_placement(new_orders, place_order_function)
        for new_order in new_orders:
            # time.sleep(5)
            self.order_book_manager.place_order(lambda new_order=new_order: place_order_function(new_order))

    # throttle order placement until response has been recieved
    # orders_to_place = len(new_orders)
    async def _async_order_placement(self, new_orders, place_order_function):
        for new_order in new_orders:
            await self.order_book_manager.place_order(lambda new_order=new_order: place_order_function(new_order))


if __name__ == '__main__':
    ErisXMarketMakerKeeper(sys.argv[1:]).main()
