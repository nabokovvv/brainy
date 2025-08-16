import httpx
import logging
import base64
import json

logger = logging.getLogger(__name__)

class SearchClient:
    def __init__(self, search_backend='yandex', api_key=None):
        self.search_backend = search_backend
        self.api_key = api_key

    async def search(self, query: str, num_results: int = 10) -> str:
        if self.search_backend == 'yandex':
            async with httpx.AsyncClient() as client:
                return await self._yandex_search(client, query, num_results)
        else:
            raise NotImplementedError(f"Search backend '{self.search_backend}' is not supported.")

    async def _yandex_search(self, client: httpx.AsyncClient, query: str, num_results: int) -> str:
        if not self.api_key:
            raise ValueError("Yandex API key is required.")
        
        url = "https://searchapi.api.cloud.yandex.net/v2/web/search"
        headers = {
            "Authorization": f"Api-Key {self.api_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "query": {
                "searchType": "SEARCH_TYPE_COM",
                "queryText": query,
                "familyMode": "FAMILY_MODE_MODERATE"
            },
            "sortSpec": {
                "sortMode": "SORT_MODE_BY_RELEVANCE"
            },
            "groupSpec": {
                "groupMode": "GROUP_MODE_DEEP",
                "groupsOnPage": num_results,
                "docsInGroup": 1
            },
            "l10N": "LOCALIZATION_EN",
            "folderId": "b1g95rft2t7d23tia331",
            "responseFormat": "FORMAT_XML"
        }
        
        response = await client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        
        try:
            response_json = response.json()
        except json.JSONDecodeError as e:
            logger.error(f"Yandex Search - JSONDecodeError: {e}. Raw response text: {response.text}")
            raise ValueError("Yandex API response was not valid JSON.") from e

        if isinstance(response_json, dict) and "rawData" in response_json:
            decoded_xml = base64.b64decode(response_json["rawData"]).decode('utf-8')
            logger.info(f"Yandex Search - Raw XML Response: {decoded_xml}")
            return decoded_xml
        else:
            logger.error(f"Yandex Search - Unexpected response structure or missing rawData. Full response: {response_json}")
            raise ValueError("Yandex API response did not contain expected rawData.")
