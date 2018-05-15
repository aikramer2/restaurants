import time
import logging
import os
import pymongo
from db.dbclient import MongoClient
from itertools import cycle
import requests
import logging
from tqdm import tqdm_notebook, tqdm
import datetime
from hashlib import md5
from pymongo import ReplaceOne

logger = logging.getLogger()
logger.setLevel(logging.INFO)


class Cycle(list):
    def __getitem__(self, key):
        try:
            return super().__getitem__(key)
        except IndexError:
            if isinstance(key, int):
                key = len(self) % key
                return super().__getitem__(key)

            else:
                raise TypeError(f"TypeError: list indices must be integers or slices, not {type(key)}")


class YelpClient:
    """
    So the yelp client lets you call the API 5000 times each day,
    and within each call youre limited to 50 results.

    Once we ingest a restaurant, we do not want it to show up in future
    searches, lest we waste a search.

    Ideally, we would have a date added field; that way we could start
    at the beginning of yelp, do a sweep to the current date, and just
    include new restaurants in our search.

    Another constraint is that search results can only be access up to the
    1000 mark.

    Therefore, in dense regions you want to narrow your search.

    We will optimize these things later.

    For now, we note that the in the densest zip code in the US,
    we get just < 1000 results if we include a radius of 1500 meters.

    Here will be the metaprocedure:

    - get previous


    """

    def __init__(self, api_key=None, delay_between_requests=.1):
        self.delay_between_requests = delay_between_requests
        self.api_key = api_key or os.environ.get('YELP_API_KEY')
        self.request_headers = {'Authorization': f'Bearer {self.api_key}'}
        self.op_client = MongoClient(collection='ops').collection
        self.result_client = MongoClient(collection='restaurant').collection
        self.op_name = "yelp-restaurant-ingest"
        self.last_ingest = self.get_last_ingest()
        self.searches = []

        self.zip_ranges = {
            'LA': range(90001, 90510),
            'Boston': range(1907, 2305)
        }

        self.zip_queue = self.get_zip_queue()

    @staticmethod
    def to_entity(i):
        try:
            result = {
                'image_url': i['image_url'],
                'name': i['name'],
                'coords': {'lat': i['coordinates']['latitude'], 'lon': i['coordinates']['longitude']},
                'phone': i['display_phone'],
                'yelp': {
                    'rating': i.get('rating'),
                    'review_count': i.get('review_count'),
                    'price': i.get('price'),
                    'categories': i.get('categories'),
                    'is_closed': False,
                    'alias': i['alias']
                }
            }
            result.update(i['location'])
            hash_content = "".join([result['name'], str(result['zip_code']).lower()])
            hash_id = md5(hash_content.encode()).hexdigest()
            result['hash_id'] = hash_id
            return result
        except Exception as e:
            print(i)
            raise e

    def get_zip_queue(self):
        zip_queue = zip(*list(self.zip_ranges.values()))
        return Cycle([zipcode for ziplist in zip_queue for zipcode in ziplist])

    def get_last_ingest(self):
        cursor = self.op_client.find({"op": self.op_name})
        try:
            return cursor.sort('date', direction=pymongo.DESCENDING).limit(1).next()
        except StopIteration:
            return {}

    def save_ingest(self):
        doc = {'date': datetime.datetime.now(), 'searches': self.searches, 'op': self.op_name}
        self.op_client.insert_one(doc)

    def _search(self, url, params, callback=None):
        """
        Going to make this function very javascripty for no reason
        """
        callback = callback or self.log
        response = requests.get(url, params=params, headers=self.request_headers)
        if response.status_code != 200:
            if callback:
                return callback(response.status_code, response.json())
        else:
            return response.json(), None

    def search(self, url, params, callback=None):
        # for a specific zip
        result, exception = self._search(url, params, callback=callback)
        results = []
        if not exception:
            logging.info(f"Got {result['total']} results, paginating...")
            results.extend([self.to_entity(i) for i in result['businesses']])
            while len(results) < result['total'] and exception is None and len(results) < 1000:
                params['offset'] += len(result['businesses'])
                result, exception = self._search(url, params, callback=callback)
                if exception is None:
                    results.extend([self.to_entity(i) for i in result['businesses']])
                time.sleep(self.delay_between_requests)
                logging.info(f"current: {len(results)}, offset: {params['offset']}")

        return results, exception

    def _to_zip(self, zipcode):
        zipcode = "0" * (5 - len(str(zipcode))) + str(zipcode)
        return zipcode

    def log(self, status_code, reason):
        logging.error(f"Got HTTP Error {status_code}: {reason}")
        return None, Exception("HTTP Error {status_code}: {reason}")

    def pick_next_zip(self):
        if len(self.searches) == 0:
            last_searches = self.last_ingest.get('searches')
            if last_searches is None:
                last_search = self.zip_queue[-1]
            else:
                last_search = last_searches[-1]
        else:
            last_search = self.searches[-1]

        idx = self.zip_queue.index(last_search) + 1
        return self.zip_queue[idx]

    @staticmethod
    def get_restaurants(notebook=False, max_zips=10):
        client = YelpClient()
        categories = "restaurants"
        limit = 50
        sort_by = 'distance'
        url = 'https://api.yelp.com/v3/businesses/search'
        exception = None
        results = []
        count = 0
        prog = tqdm_notebook(1e9) if notebook else tqdm(1e9)

        while exception is None and count < max_zips:
            zipcode = client.pick_next_zip()
            logging.info(f"Searching for zipcode: {zipcode}")
            params = {
                "limit": limit,
                "sort_by": sort_by,
                "categories": categories,
                "location": client._to_zip(zipcode),
                'offset': 0,
                'locale': 'en_US',
            }
            restaurants, exception = client.search(url, params, client.log)
            client.searches.append(zipcode)

            if exception is None:
                results.extend(restaurants)
                count += 1
                prog.update(len(restaurants))

        client.upload_results(results, notebook=notebook)
        return results

    def upload_results_slow(self, results, notebook=True):
        logging.info("Uploading results")
        prog = tqdm_notebook if notebook else tqdm
        for result in prog(results, total=len(results)):
            self.result_client.replace_one({"hash_id": result['hash_id']}, result, upsert=True)
        self.save_ingest()

    def upload_results(self, results, notebook=True):
        logging.info("Uploading results")


        operations = [ReplaceOne({"hash_id": result['hash_id']}, result, upsert=True) for result in results]
        records = len(operations)
        if records > 0:
            self.result_client.bulk_write(operations)
            logging.info(f"Wrote {records} to db.")
        else:
            logging.warning("Wrote No records to db, FAILURE FAILURE RED ALERT.")

        self.save_ingest()
