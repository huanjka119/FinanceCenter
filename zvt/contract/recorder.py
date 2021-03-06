# -*- coding: utf-8 -*-
import logging
import time
import uuid
from typing import List
import multiprocessing
import random

import pandas as pd
from sqlalchemy.orm import Session
from tqdm import tqdm

from zvt import zvt_env
from zvt.domain import StockTradeDay, StockDetail
from zvt.contract import IntervalLevel, Mixin, EntityMixin
from zvt.contract.api import get_db_session, get_schema_columns
from zvt.contract.api import get_entities, get_data
from zvt.contract.common import Region, Provider, EntityType
from zvt.utils.time_utils import to_pd_timestamp, TIME_FORMAT_DAY, to_time_str, \
                                 evaluate_size_from_timestamp, is_in_same_interval, \
                                 now_pd_timestamp, count_mins_before_close_time, \
                                 is_same_date, date_delta
from zvt.utils.utils import fill_domain_from_dict
from zvt.utils.request_utils import get_http_session, jq_swap_account, jq_get_query_count


class Meta(type):
    def __new__(meta, name, bases, class_dict):
        cls = type.__new__(meta, name, bases, class_dict)
        # register the recorder class to the data_schema
        if hasattr(cls, 'data_schema') and hasattr(cls, 'provider'):
            if cls.data_schema and issubclass(cls.data_schema, Mixin):
                # print(f'{cls.__name__}:{cls.data_schema.__name__}')
                cls.data_schema.register_recorder_cls(cls.provider, cls)
        return cls


class Recorder(metaclass=Meta):
    logger = logging.getLogger(__name__)

    # overwrite them to setup the data you want to record
    region: Region = None
    provider: Provider = Provider.Default
    data_schema: Mixin = None

    url = None

    def __init__(self,
                 batch_size: int = 10,
                 force_update: bool = False,
                 sleeping_time: int = 10) -> None:
        """

        :param batch_size:batch size to saving to db
        :type batch_size:int
        :param force_update: whether force update the data even if it exists,please set it to True if the data need to
        be refreshed from the provider
        :type force_update:bool
        :param sleeping_time:sleeping seconds for recoding loop
        :type sleeping_time:int
        """
        self.logger = logging.getLogger(self.__class__.__name__)

        assert self.provider.value is not None
        assert self.data_schema is not None
        assert self.provider in self.data_schema.providers[self.region]

        self.batch_size = batch_size
        self.force_update = force_update
        self.sleeping_time = sleeping_time

        # using to do db operations
        self.session = get_db_session(region=self.region, 
                                      provider=self.provider,
                                      data_schema=self.data_schema)

    def run(self):
        raise NotImplementedError

    def sleep(self):
        if self.sleeping_time > 0:
            self.logger.info(f'sleeping {self.sleeping_time} seconds')
            time.sleep(self.sleeping_time)


class RecorderForEntities(Recorder):
    # overwrite them to fetch the entity list
    entity_provider: Provider = Provider.Default
    entity_schema: EntityMixin = None

    def __init__(self,
                 entity_type: EntityType = EntityType.Stock,
                 exchanges=['sh', 'sz'],
                 entity_ids=None,
                 codes=None,
                 batch_size=10,
                 force_update=False,
                 sleeping_time=10,
                 share_para=None) -> None:
        """
        :param entity_type:
        :type entity_type:
        :param exchanges:
        :type exchanges:
        :param entity_ids: set entity_ids or (entity_type,exchanges,codes)
        :type entity_ids:
        :param codes:
        :type codes:
        :param batch_size:
        :type batch_size:
        :param force_update:
        :type force_update:
        :param sleeping_time:
        :type sleeping_time:
        """
        self.region = share_para[4]

        super().__init__(batch_size=batch_size, force_update=force_update, sleeping_time=sleeping_time)
        
        assert self.entity_provider.value is not None
        assert self.entity_schema is not None

        # setup the entities you want to record
        self.entity_type = entity_type
        self.exchanges = exchanges
        self.codes = codes
        self.share_para = share_para

        # set entity_ids or (entity_type,exchanges,codes)
        self.entity_ids = entity_ids

        self.entity_session: Session = None
        self.entities: List = None
        self.init_entities()

    def init_entities(self):
        """
        init the entities which we would record data for

        """
        assert self.region is not None

        if self.entity_provider == self.provider and self.entity_schema == self.data_schema:
            self.entity_session = self.session
        else:
            self.entity_session = get_db_session(region=self.region, 
                                                 provider=self.entity_provider, 
                                                 data_schema=self.entity_schema)

        # init the entity list
        self.entities = get_entities(region=self.region,
                                     session=self.entity_session,
                                     entity_schema=self.entity_schema,
                                     entity_type=self.entity_type,
                                     exchanges=self.exchanges,
                                     entity_ids=self.entity_ids,
                                     codes=self.codes,
                                     return_type='domain',
                                     provider=self.entity_provider)


class TimeSeriesDataRecorder(RecorderForEntities):
    def __init__(self,
                 entity_type: EntityType = EntityType.Stock,
                 exchanges=['sh', 'sz'],
                 entity_ids=None,
                 codes=None,
                 batch_size=10,
                 force_update=False,
                 sleeping_time=5,
                 default_size=2000,
                 real_time=False,
                 fix_duplicate_way='add',
                 start_timestamp=None,
                 end_timestamp=None,
                 close_hour=0,
                 close_minute=0,
                 share_para=None) -> None:

        self.default_size = default_size
        self.real_time = real_time

        self.close_hour = close_hour
        self.close_minute = close_minute

        self.fix_duplicate_way = fix_duplicate_way
        self.start_timestamp = to_pd_timestamp(start_timestamp)
        self.end_timestamp = to_pd_timestamp(end_timestamp)

        super().__init__(entity_type, exchanges, entity_ids, codes, batch_size, force_update, sleeping_time, share_para=share_para)

    def get_latest_saved_record(self, entity):
        order = eval('self.data_schema.{}.desc()'.format(self.get_evaluated_time_field()))

        records = get_data(region=self.region,
                           entity_id=entity.id,
                           provider=self.provider,
                           data_schema=self.data_schema,
                           order=order,
                           limit=1,
                           return_type='domain',
                           session=self.session)
        if records:
            return records[0]
        return None

    def evaluate_start_end_size_timestamps(self, now, entity, trade_day, stock_detail, http_session):
        # not to list date yet
        # print("step 1: entity.timestamp:{}".format(entity.timestamp))
        trade_index = 0
        if entity.timestamp and (entity.timestamp >= now):
            trade = trade_day[trade_index] if len(trade_day) > 0 else None
            return entity.timestamp, None, trade, 0, None

        
        latest_saved_record = self.get_latest_saved_record(entity=entity)
        # print("step 2: latest_saved_record:{}".format(latest_saved_record))

        if latest_saved_record:
            latest_timestamp = eval('latest_saved_record.{}'.format(self.get_evaluated_time_field()))
        else:
            latest_timestamp = entity.timestamp
        # print("step 3: latest_timestamp:{}".format(latest_timestamp))

        if not latest_timestamp:
            trade = trade_day[trade_index] if len(trade_day) > 0 else None
            return self.start_timestamp, self.end_timestamp, trade, self.default_size, None

        # print("step 4: start_timestamp:{}, end_timestamp:{}".format(self.start_timestamp, self.end_timestamp))
        if self.start_timestamp:
            latest_timestamp = max(latest_timestamp, self.start_timestamp)
        # print("step 5: latest_timestamp:{}".format(latest_timestamp))
            
        size = self.default_size
        if self.end_timestamp:
            if latest_timestamp >= self.end_timestamp:
                size = 0
        else:
            size = (now.replace(hour=0, minute=0, second=0) - latest_timestamp).days

        trade = trade_day[trade_index] if len(trade_day) > 0 else None
        return latest_timestamp, self.end_timestamp, trade, size, None

    def get_data_map(self):
        """
        {'original_field':('domain_field',transform_func)}

        """
        return {}

    def record(self, entity, start, end, size, timestamps, http_session):
        """
        implement the recording logic in this method, should return json or domain list

        :param entity:
        :type entity:
        :param start:
        :type start:
        :param end:
        :type end:
        :param size:
        :type size:
        :param timestamps:
        :type timestamps:
        """
        raise NotImplementedError

    def get_evaluated_time_field(self):
        """
        the timestamp field for evaluating time range of recorder,used in get_latest_saved_record

        """
        return 'timestamp'

    def get_original_time_field(self):
        return 'timestamp'

    def generate_domain_id(self, entity, original_data, time_fmt=TIME_FORMAT_DAY):
        """
        generate domain id from the entity and original data,the default id meaning:entity + event happen time

        :param entity:
        :type entity:
        :param original_data:
        :type original_data:
        :param time_fmt:
        :type time_fmt:
        :return:
        :rtype:
        """
        timestamp = to_time_str(original_data[self.get_original_time_field()], fmt=time_fmt)
        return "{}_{}".format(entity.id, timestamp)

    def generate_domain(self, entity, original_data):
        """
        generate the data_schema instance using entity and original_data,the original_data is from record result

        :param entity:
        :param original_data:
        """

        got_new_data = False

        # if the domain is directly generated in record method, we just return it
        if isinstance(original_data, self.data_schema):
            got_new_data = True
            return got_new_data, original_data

        the_id = self.generate_domain_id(entity, original_data)

        # optional way
        # item = self.session.query(self.data_schema).get(the_id)

        items = get_data(region=self.region, data_schema=self.data_schema, session=self.session, provider=self.provider,
                         entity_id=entity.id, filters=[self.data_schema.id == the_id], return_type='domain')

        if items and not self.force_update:
            self.logger.info('ignore the data {}:{} saved before'.format(self.data_schema.__name__, the_id))
            return got_new_data, None

        if not items:
            timestamp_str = original_data[self.get_original_time_field()]
            timestamp = None
            try:
                timestamp = to_pd_timestamp(timestamp_str)
            except Exception as e:
                self.logger.exception(e)

            if 'name' in get_schema_columns(self.data_schema):
                domain_item = self.data_schema(id=the_id,
                                               code=entity.code,
                                               name=entity.name,
                                               entity_id=entity.id,
                                               timestamp=timestamp)
            else:
                domain_item = self.data_schema(id=the_id,
                                               code=entity.code,
                                               entity_id=entity.id,
                                               timestamp=timestamp)
            got_new_data = True
        else:
            domain_item = items[0]

        fill_domain_from_dict(domain_item, original_data, self.get_data_map())
        return got_new_data, domain_item

    def persist(self, entity, domain_list):
        """
        persist the domain list to db

        :param entity:
        :param domain_list:
        """
        if domain_list:
            try:
                if domain_list[0].timestamp >= domain_list[-1].timestamp:
                    first_timestamp = domain_list[-1].timestamp
                    last_timestamp = domain_list[0].timestamp
                else:
                    first_timestamp = domain_list[0].timestamp
                    last_timestamp = domain_list[-1].timestamp
            except:
                first_timestamp = domain_list[0].timestamp
                last_timestamp = domain_list[-1].timestamp

            self.logger.info(
                "persist {} for entity_id:{},time interval:[{}, {}]".format(
                    self.data_schema.__name__, entity.id, first_timestamp, last_timestamp))

            self.session.add_all(domain_list)
            self.session.commit()

    def on_finish(self):
        try:
            if self.session:
                self.session.close()

            if self.entity_session:
                self.entity_session.close()
        except Exception as e:
            self.logger.error(e)
        

    def on_finish_entity(self, entity, http_session):
        pass

    def process_duplicate(self, original_list, entity_item):
        all_duplicated = True

        if original_list:
            domain_list = []
            for original_item in original_list:
                got_new_data, domain_item = self.generate_domain(entity_item, original_item)

                if got_new_data:
                    all_duplicated = False

                # handle the case generate_domain_id generate duplicate id
                if domain_item:
                    duplicate = [item for item in domain_list if item.id == domain_item.id]
                    if duplicate:
                        # ignore
                        if self.fix_duplicate_way != 'add':
                            return True, all_duplicated
                        # regenerate the id
                        domain_item.id = "{}_{}".format(domain_item.id, uuid.uuid1())
                    domain_list.append(domain_item)

            if domain_list:
                self.persist(entity_item, domain_list)
            else:
                self.logger.info('just got {} duplicated data in this cycle'.format(len(original_list)))
        return False, all_duplicated

    def process_realtime(self, entity_item, original_list, all_duplicated, now, http_session):
        entity_finished = False
        # could not get more data
        if not original_list or all_duplicated:
            # not realtime
            if not self.real_time:
                entity_finished = True

            # realtime and to the close time
            elif (self.close_hour is not None) and (self.close_minute is not None):
                if now.hour >= self.close_hour:
                    if now.minute - self.close_minute >= 5:
                        self.logger.info('{} now is the close time: {}'.format(entity_item.id, now))
                        entity_finished = True
        
        # add finished entity to finished_items
        if entity_finished:
            self.on_finish_entity(entity_item, http_session)
            return True

        return False

    def process_entity(self, entity_item, trade_day, stock_detail, http_session):
        step1 = time.time()
        now = now_pd_timestamp(self.region)

        start_timestamp, end_timestamp, end_date, size, timestamps = \
            self.evaluate_start_end_size_timestamps(now, entity_item, trade_day, stock_detail, http_session)
        size = int(size)
        # self.logger.info("evaluate entity_item:{}, time cost:{}".format(entity_item.id, time.time()-step1))

        # no more to record
        if size == 0:
            start = start_timestamp.strftime('%Y-%m-%d') if start_timestamp else None
            # self.logger.info("no update {} {}, {}, cost: {}".format(
            #     self.data_schema.__name__, start_timestamp, entity_item.id, time.time()-step1))
            self.on_finish_entity(entity_item, http_session)
            return True

        # fetch and save
        start = start_timestamp.strftime('%Y-%m-%d') if start_timestamp else None
        trade_day = trade_day[0].strftime('%Y-%m-%d') if trade_day else None
        end = end_date.strftime('%Y-%m-%d') if end_date else None
        self.logger.info('request {}, {}, {}, {}, {}, {}'.format(entity_item.id, size, jq_get_query_count(), trade_day, start, end))
        original_list = self.record(entity_item, start=start_timestamp, end=end_timestamp, size=size,
                                    timestamps=timestamps, http_session=http_session)        
        # self.logger.info("record entity_item:{}, time cost:{}".format(entity_item.id, time.time()-step1))

        # handle duplicate items
        entity_finished, all_duplicated = self.process_duplicate(original_list, entity_item)
        if entity_finished:
            # self.logger.info("ignore original duplicate item: {}, time cost: {}".format(domain_item.id, time.time()-step1))
            return True

        # handle realtime items
        entity_finished = self.process_realtime(entity_item, original_list, all_duplicated, now, http_session)
        if entity_finished:
            # if zvt_env['zvt_debug']:
            #     latest_saved_record = self.get_latest_saved_record(entity=entity_item)
            #     if latest_saved_record:
            #         start_timestamp = eval('latest_saved_record.{}'.format(self.get_evaluated_time_field()))
            #     self.logger.info("finish recording {} id: {}, latest_timestamp: {}, time cost: {}".format(
            #         self.data_schema.__name__, entity_item.id, start_timestamp, time.time()-step1))
            # else:
            #     self.logger.info("finish recording {} id: {}, time cost: {}".format(
            #         self.data_schema.__name__, entity_item.id, time.time()-step1))
            return True

        self.logger.info("update recording {} id: {}, time cost: {}".format(
            self.data_schema.__name__, entity_item.id, time.time()-step1))
        return False

    def process_loop(self, entity_item, trade_day, stock_detail, http_session):
        while True:
            try:
                if self.process_entity(entity_item, trade_day, stock_detail, http_session):
                    return
                # sleep for a while to next entity
                self.sleep()
            except Exception as e:
                self.logger.exception("recording data id:{}, {}, error:{}".format(entity_item.id, self.data_schema, e))
                return

    def run(self):
        http_session = get_http_session()
        trade_days= StockTradeDay.query_data(region=self.region, order=StockTradeDay.timestamp.desc(), return_type='domain')
        trade_day = [day.timestamp for day in trade_days]
        stock_detail = StockDetail.query_data(region=self.region, columns=['entity_id', 'end_date'], index=['entity_id'], return_type='df')

        time.sleep(random.randint(0, self.share_para[1]))
        process_identity = multiprocessing.current_process()._identity
        if len(process_identity) > 0:
            #  The worker process tqdm bar shall start at Position 1
            worker_id = (process_identity[0]-1)%self.share_para[1] + 1
        else:
            worker_id = 0
        desc = "{:02d}: {}".format(worker_id, self.share_para[0])

        with tqdm(total=len(self.entities), ncols=80, position=worker_id, desc=desc, leave=self.share_para[3]) as pbar:
            for entity_item in self.entities:
                self.process_loop(entity_item, trade_day, stock_detail, http_session)
                self.share_para[2].acquire()
                pbar.update()
                self.share_para[2].release()
        self.on_finish()


class FixedCycleDataRecorder(TimeSeriesDataRecorder):
    def __init__(self,
                 entity_type: EntityType = EntityType.Stock,
                 exchanges=['sh', 'sz'],
                 entity_ids=None,
                 codes=None,
                 batch_size=10,
                 force_update=True,
                 sleeping_time=10,
                 default_size=2000,
                 real_time=False,
                 fix_duplicate_way='ignore',
                 start_timestamp=None,
                 end_timestamp=None,
                 close_hour=0,
                 close_minute=0,
                 # child add
                 level=IntervalLevel.LEVEL_1DAY,
                 kdata_use_begin_time=False,
                 one_day_trading_minutes=24 * 60,
                 share_para=None) -> None:
        super().__init__(entity_type, exchanges, entity_ids, codes, batch_size, force_update, 
                         sleeping_time, default_size, real_time, fix_duplicate_way, start_timestamp, 
                         end_timestamp, close_hour, close_minute, share_para=share_para)

        self.level = IntervalLevel(level)
        self.kdata_use_begin_time = kdata_use_begin_time
        self.one_day_trading_minutes = one_day_trading_minutes

    def get_latest_saved_record(self, entity):
        # step = time.time()
        order = eval('self.data_schema.{}.desc()'.format(self.get_evaluated_time_field()))
        # self.logger.info("get order: {}".format(time.time()-step))

        # 对于k线这种数据，最后一个记录有可能是没完成的，所以取两个，总是删掉最后一个数据，更新之
        # self.logger.info("record info: {}, {}, {}".format(entity.id, order, self.level))
        records = get_data(region=self.region,
                           entity_id=entity.id,
                           provider=self.provider,
                           data_schema=self.data_schema,
                           order=order,
                           limit=2,
                           return_type='domain',
                           session=self.session,
                           level=self.level)
        # self.logger.info("get record: {}".format(time.time()-step))

        if records:
            # delete unfinished kdata
            if len(records) == 2:
                if is_in_same_interval(t1=records[0].timestamp, t2=records[1].timestamp, level=self.level):
                    self.session.delete(records[0])
                    self.session.flush()
                    return records[1]
            return records[0]
        return None

    def evaluate_start_end_size_timestamps(self, now, entity, trade_day, stock_detail, http_session):
        # not to list date yet
        # step1 = time.time()
        trade_index = 0

        if entity.timestamp and (entity.timestamp >= now):
            trade = trade_day[trade_index] if len(trade_day) > 0 else None
            return entity.timestamp, None, trade, 0, None

        # get latest record
        latest_saved_record = self.get_latest_saved_record(entity=entity)
        # self.logger.info("step 1: get latest save record: {}".format(time.time()-step1))

        if latest_saved_record:
            # the latest saved timestamp
            latest_saved_timestamp = latest_saved_record.timestamp
        else:
            # the list date
            latest_saved_timestamp = entity.timestamp
        # print("step 3: latest_saved_timestamp:{}".format(latest_saved_timestamp))

        # print("step 4: start_timestamp:{}, end_timestamp:{}".format(self.start_timestamp, self.end_timestamp))
        
        if not latest_saved_timestamp:
            trade = trade_day[trade_index] if len(trade_day) > 0 else None
            return None, None, trade, self.default_size, None
        
        # self.logger.info("latest_saved_timestamp:{}, tradedays:{}".format(latest_saved_timestamp, trade_day[:2]))
        
        if trade_day is not None and len(trade_day) > 0:
            count_mins = count_mins_before_close_time(now, self.close_hour, self.close_minute)
            if count_mins > 0 and is_same_date(trade_day[0], now):
                trade_index = 1
        
        # self.logger.info("step 2: get trade index: {}".format(time.time()-step1))

        try:
            end_date = stock_detail.loc[entity.id].at['end_date']
            days = date_delta(now, end_date)
        except Exception as e:
            # self.logger.warning("can't find stock in stock detail:{}".format(e))
            days = -1

        # self.logger.info("step 3: get end date: {}".format(time.time()-step1))

        if days > 0:
            try:
                trade_index = trade_day.index(end_date)
                # self.logger.info("entity:{}, index:{}, out of market at date:{}, index_day:{}".format(entity.id, trade_index, end_date, trade_day[trade_index]))
            except Exception as _:
                try:
                    trade_index = trade_day.index[trade_day.index < end_date].index[0]
                except Exception as e:
                    self.logger.warning("can't find timestamp:{} between trade_day:{}".format(end_date, e))
                
        size = evaluate_size_from_timestamp(start_timestamp=latest_saved_timestamp, 
                                            end_timestamp=now,
                                            level=self.level,
                                            one_day_trading_minutes=self.one_day_trading_minutes,
                                            trade_day=trade_day[trade_index:])

        # self.logger.info("step 4: evaluate: {}".format(time.time()-step1))
        trade = trade_day[trade_index] if len(trade_day) > 0 else None
        return latest_saved_timestamp, None, trade, size, None 


class TimestampsDataRecorder(TimeSeriesDataRecorder):

    def __init__(self,
                 entity_type: EntityType = EntityType.Stock,
                 exchanges=['sh', 'sz'],
                 entity_ids=None,
                 codes=None,
                 batch_size=10,
                 force_update=False,
                 sleeping_time=5,
                 default_size=2000,
                 real_time=False,
                 fix_duplicate_way='add',
                 start_timestamp=None,
                 end_timestamp=None,
                 close_hour=0,
                 close_minute=0,
                 share_para=None) -> None:
        super().__init__(entity_type, exchanges, entity_ids, codes, batch_size, force_update, sleeping_time,
                         default_size, real_time, fix_duplicate_way, start_timestamp, end_timestamp,
                         close_hour=close_hour, close_minute=close_minute, share_para=share_para)
        self.security_timestamps_map = {}

    def init_timestamps(self, entity_item, http_session) -> List[pd.Timestamp]:
        raise NotImplementedError

    def evaluate_start_end_size_timestamps(self, now, entity, trade_day, stock_detail, http_session):
        trade_index = 0
        timestamps = self.security_timestamps_map.get(entity.id)
        if not timestamps:
            timestamps = self.init_timestamps(entity, http_session)
            if self.start_timestamp:
                timestamps = [t for t in timestamps if t >= self.start_timestamp]

            if self.end_timestamp:
                timestamps = [t for t in timestamps if t <= self.end_timestamp]

            self.security_timestamps_map[entity.id] = timestamps

        if not timestamps:
            trade = trade_day[trade_index] if len(trade_day) > 0 else None
            return None, None, trade, 0, timestamps

        timestamps.sort()

        latest_record = self.get_latest_saved_record(entity=entity)
        trade = trade_day[trade_index] if len(trade_day) > 0 else None

        if latest_record:
            # self.logger.info('latest record timestamp:{}'.format(latest_record.timestamp))
            timestamps = [t for t in timestamps if t >= latest_record.timestamp]

            if timestamps:
                return timestamps[0], timestamps[-1], trade, len(timestamps), timestamps
            return None, None, trade, 0, None

        return timestamps[0], timestamps[-1], trade, len(timestamps), timestamps


__all__ = ['Recorder', 'RecorderForEntities', 'FixedCycleDataRecorder', 'TimestampsDataRecorder',
           'TimeSeriesDataRecorder']
