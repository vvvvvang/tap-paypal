#!/usr/bin/env python3

'''A Singer tap for extracting transactions data from the PayPal Sync API.'''

import os
import json
from datetime import datetime
import dateutil.parser
from dateutil.relativedelta import relativedelta
import pytz
import requests
import singer
from singer import metrics
from singer import utils

REQUIRED_CONFIG_KEYS = ['client_id', 'client_secret']
LOGGER = singer.get_logger()
STREAMS = {}

def stream_is_selected(metadata):
    '''Checks the metadata and returns if a stream is selected or not.'''
    return metadata.get((), {}).get('selected', False)

def get_abs_path(path):
    '''Returns absolute file path.'''
    return os.path.join(os.path.dirname(os.path.realpath(__file__)), path)

def load_json(path):
    '''Load a JSON file as a dictionary.'''
    with open(path, 'r') as file:
        json_ = json.load(file)
    return json_

def load_schema(schema_name):
    '''Load a single schema in ./schemas by stream name as a dictionary.'''
    path = os.path.join(get_abs_path('schemas'), schema_name + '.json')
    schema = load_json(path)
    return schema

def load_all_schemas():
    '''Load each schema in ./schemas into a dictionary with stream name keys.'''
    schemas = {}
    for filename in os.listdir(get_abs_path('schemas')):
        path = get_abs_path('schemas') + '/' + filename
        file_raw = filename.replace('.json', '')
        schemas[file_raw] = load_json(path)
    return schemas

def discover():
    '''
    Generates the catalog dictionary containing all metadata and
    automatically selects all fields to be included.
    '''
    raw_schemas = load_all_schemas()
    entries = []

    for schema_name, schema in raw_schemas.items():
        top_level_metadata = {
            'inclusion': 'available',
            'selected': True,
            'forced-replication-method': 'INCREMENTAL',
            'valid-replication-keys': ['transaction_updated_date']}

        if schema_name == 'invoices':
            top_level_metadata['forced-replication-method'] = 'FULL_TABLE'
            del top_level_metadata['valid-replication-keys']

        metadata = singer.metadata.new()
        for key, val in top_level_metadata.items():
            metadata = singer.metadata.write(
                compiled_metadata=metadata,
                breadcrumb=(),
                k=key,
                val=val)

        for key in schema['properties'].keys():
            metadata = singer.metadata.write(
                compiled_metadata=metadata,
                breadcrumb=('properties', key),
                k='inclusion',
                val='automatic')

        catalog_entry = {
            'stream': schema_name,
            'tap_stream_id': schema_name,
            'schema': schema,
            'metadata': singer.metadata.to_list(metadata),
            'key_properties': ['transaction_id'],
            'bookmark_properties': ['transaction_updated_date']
        }
        entries.append(catalog_entry)

    return {'streams': entries}

class Stream:
    '''
    Stores schema data and a buffer of records for a particular stream.
    When a Stream's buffer reaches its `buffer_size`, it returns a
    boolean that the buffer should be emptied. Emptying the buffer resets the
    buffer and writes all records to stdout.

    Also tracks a bookmark for each record written out of the buffer.
    '''
    buffer_limit = 100

    def __init__(self, stream, bookmark=None):
        self.stream = stream
        self.stream_name = stream.tap_stream_id
        self.schema = stream.schema.to_dict()
        self.key_properties = stream.key_properties
        self.counter = metrics.record_counter(self.stream_name)
        self.buffer = []
        self.bookmark = bookmark

    def add_to_buffer(self, record):
        '''Add a record to the buffer and return True if the buffer is full.'''
        self.buffer.append(record)
        return bool(len(self.buffer) >= self.buffer_limit)

    def empty_buffer(self, state):
        '''Empties the buffer, writing records to stdout and saving state.'''
        for record in self.buffer:
            transformed = singer.transform(record, self.schema)
            singer.write_record(self.stream_name, transformed)
            self.bookmark = dateutil.parser \
                .parse(record['transaction_updated_date'])
            self.counter.increment()
        self.buffer = []
        self.save_state(state)

    def save_state(self, state):
        '''Writes bookmark to state and writes state to stdout.'''
        state = singer.write_bookmark(
            state=state,
            tap_stream_id=self.stream_name,
            key='transaction_updated_date',
            val=self.bookmark.isoformat('T'))
        singer.write_state(state)

    def write_schema(self):
        '''Writes formatted schema to stdout.'''
        singer.write_schema(
            self.stream_name,
            self.schema,
            self.key_properties)

class BatchWriter:
    '''
    Sorts a batch of transactions by stream, filtering out any transactions
    that don't meet replication key requirements, and writing the remainder
    to the appropriate stream.
    '''
    def __init__(self, state):
        self.state = state

    def process(self, batch):
        '''
        Sorts transaction data into the appropriate stream buffers, emptying the
        buffers whenever they become full.
        '''
        for transaction in batch:
            updated_date = \
                transaction['transaction_info']['transaction_updated_date']
            id_ = transaction['transaction_info']['transaction_id']
            for stream_name, record in transaction.items():
                if record:
                    record['transaction_id'] = id_
                    record['transaction_updated_date'] = updated_date
                    self.write_to_stream(stream_name, record)

    def write_to_stream(self, stream_name, record):
        '''Writes valid record to buffer, emptying buffer if buffer is full.'''
        updated_date = dateutil.parser.parse(
            record['transaction_updated_date'])
        stream = STREAMS[stream_name]
        if updated_date >= stream.bookmark:
            buffer_is_full = stream.add_to_buffer(record)
            if buffer_is_full:
                stream.empty_buffer(self.state)

def empty_all_buffers(state):
    '''Called at end of sync, empties remaining records in stream buffers.'''
    for stream in STREAMS.values():
        stream.empty_buffer(state)
    for stream in STREAMS.values():
        LOGGER.info(
            "Completed sync of %s records to stream '%s'.",
            stream.counter.value,
            stream.stream_name)

def process_transactions(client, state, start_date, end_date=None, fields='all'):
    '''
    Divides the date range into segments no longer than one month
    and iterates through them to request transaction batches and process them.
    '''
    if end_date is None:
        end_date = datetime.utcnow().replace(microsecond=0, tzinfo=pytz.utc)

    writer = BatchWriter(state)
    batch_size = relativedelta(months=+1, seconds=-1)
    while start_date + batch_size < end_date:
        batch_end_date = start_date + batch_size
        batches = client.get_transactions(
            start_date=start_date.isoformat('T'),
            end_date=batch_end_date.isoformat('T'),
            fields=fields)
        for batch in batches:
            writer.process(batch)
        start_date = batch_end_date + relativedelta(seconds=+1)
    batches = client.get_transactions(
        start_date=start_date.isoformat('T'),
        end_date=end_date.isoformat('T'),
        fields=fields)
    for batch in batches:
        writer.process(batch)
    empty_all_buffers(state)

def process_invoices(client, state, start_date):

    pass

def build_stream(catalog_stream, state):
    '''Generates a new stream instance and adds to the global dictionary.'''
    if state:
        bookmark = singer.get_bookmark(
            state=state,
            tap_stream_id=catalog_stream.tap_stream_id,
            key='transaction_updated_date')
        bookmark = dateutil.parser.parse(bookmark)
    else:
        # PayPal's oldest data is July 2016
        bookmark = datetime(2016, 7, 1, tzinfo=pytz.utc)
    stream = Stream(catalog_stream, bookmark)
    STREAMS[catalog_stream.tap_stream_id] = stream

def get_selected_streams(catalog):
    '''Using the catalog, returns a list of selected stream names.'''
    selected_stream_names = []
    for stream in catalog.streams:
        metadata = singer.metadata.to_map(stream.metadata)
        if stream_is_selected(metadata):
            selected_stream_names.append(stream.tap_stream_id)
    return selected_stream_names

def sync(config, state, catalog):
    '''
    Builds the streams dictionary and API client, gets the oldest bookmark of
    all of the streams to use for the API call, then kicks off the sync.
    '''
    client = PayPalClient(config)
    selected_stream_names = get_selected_streams(catalog)
    for catalog_stream in catalog.streams:
        if catalog_stream.tap_stream_id in selected_stream_names:
            build_stream(catalog_stream, state)
            STREAMS[catalog_stream.tap_stream_id].write_schema()
    oldest_updated_date = min([stream.bookmark for stream in STREAMS.values()])
    get_records(
        client=client,
        state=state,
        start_date=oldest_updated_date,
        fields=selected_stream_names)

@utils.handle_top_exception(LOGGER)
def main():
    '''Based on command line arguments, chooses discover or sync.'''
    args = utils.parse_args(REQUIRED_CONFIG_KEYS)
    if args.discover:
        catalog = discover()
        print(json.dumps(catalog, indent=2))
    elif args.catalog:
        catalog = args.catalog
        sync(args.config, args.state, catalog)

if __name__ == "__main__":
    main()
