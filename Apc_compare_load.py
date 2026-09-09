import pandas as pd
import numpy as np
import requests 
from io import StringIO
import os
import lakes
from datetime import datetime, timedelta, date

project_name = '' # 유저 입력
api_key = '' # 유저 입력
api_name = 'm15-cmp-apc-modeling-table3'

mrg_data = []
page_no = 1

while True:
  for i in range(5):
    url = f'http://dp.skhynix.com:8080/datahub/v1/api/{project_name}/{api_name}/page' # 유저 입력
    headers = {"Content-Type":"application/json", "h-api-token":api_key}
    body = {"pageNumber":page_no, "pageSize":10000, "sortBy":"RAWID", "sortOrder":"ASC"}
    data = json.dumps(body)
    response = requests.post(url, headers=headers, data=data)
    if response.status_code == 200:
      break
    if i >= 4:
      raise Exception('H-API Error')
  data_one = pd.DataFrame(json.loads(response.text)['Content'])

  if data_one.shape[0] == 0:
    break
  mrg_data.append(data_one)
  page_no += 1
data = pd.concat(mrg_data)
apc_modeling_db = data


                          
