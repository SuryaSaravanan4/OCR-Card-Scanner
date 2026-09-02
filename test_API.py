import easyocr
import os
import requests
import re

image_path = os.path.join(os.path.expanduser('~'), 'Downloads', 'magic.jpg')

reader = easyocr.Reader(['en'])
results = reader.readtext(image_path)

if results:
    raw_card_name = results[0][1]
    clean_card_name = re.sub(r'[\d\{\}]+.*$', '', raw_card_name).strip()
    
    print(f"Raw OCR: {raw_card_name}")
    print(f"Cleaned card name: {clean_card_name}")

    # Scryfall requires a custom User-Agent string
    headers = {
        'User-Agent': 'MTGOcrApp/1.0 (contact: yourname@example.com)',
        'Accept': 'application/json'
    }

    url = f"https://api.scryfall.com/cards/named?fuzzy={requests.utils.quote(clean_card_name)}"
    response = requests.get(url, headers=headers)
    data = response.json()

    if response.status_code == 200 and data.get('object') != 'error':
        print(f"\nCard: {data.get('name')}")
        print(f"Type: {data.get('type_line')}")
        print(f"Mana Cost: {data.get('mana_cost')}")
        
        prices = data.get('prices', {})
        print(f"\nPrices:")
        print(f"  Normal:  ${prices.get('usd', 'N/A')}")
        print(f"  Foil:    ${prices.get('usd_foil', 'N/A')}")
    else:
        print(f"\nAPI Error: {data.get('details', 'Card not found on Scryfall.')}")
else:
    print("No text detected in the image.")