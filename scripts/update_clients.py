import re
import yaml

clients = [
    ("AMKZ_Hyundai Ekibastyz", "act_1686577352758298"),
    ("Cadillac Qazaqstan ADS", "act_921465523570954"),
    ("AMKZ_MTZ_Virazh", "act_849607084339047"),
    ("InnovateX", "act_1448896328939955"),
    ("AMKZ_Jetour_Ekibastuz", "act_1561318098348453"),
    ("AMKZ_Changan_Pavlodar", "act_1789548078344731"),
    ("AMKZ_JetQ/Daagency", "act_1499261374602215"),
    ("AMKZ_Chery_Pavlodar", "act_1841381649802821"),
    ("AMKZ_Canon", "act_1386629536226419"),
    ("AMKZ_Haval_Aktau", "act_1925912145007648"),
    ("AMKZ_Chevrolet_Ekibastuz", "act_1404420594684720"),
    ("AMKZ_Tank_Aktau", "act_1189393596031096"),
    ("AMKZ_Jack_Ekibastuz", "act_1480178203840836"),
    ("AMKZ_Kia_Ekibastuz", "act_1569759977560277"),
    ("AMKZ_KIA_Aktau", "act_2048080829422446"),
    ("AMKZ_da_AutoTrade", "act_890824327218188"),
    ("AMK", "act_1202497118763581"),
    ("AMKZ_Changan_Aktau", "act_1451761906538097"),
    ("AMKZ_La_Oil_Kazakhstan", "act_1661050368502356"),
    ("AMKZ_Cadillac_Kazakhstan", "act_4446485905668216"),
    ("AMKZ_JetQ Astana", "act_2870993986567599"),
    ("AMKZ_Audi_Astana", "act_1376683761189514"),
    ("AMKZ_Toyota_Aktau", "act_1461646398505596"),
    ("AMKZ_Changan_Karaganda", "act_3143365589186571"),
    ("AMKZ_Geely_Astana", "act_1515002436462007"),
    ("AMKZ_Lexus_Aktau", "act_943354138604291"),
    ("AMKZ_Geely_Kostanay", "act_1569970754784741"),
    ("AMKZ_Geely_Karaganda", "act_1035642672667694"),
    ("Unknown_120996931604569", "act_120996931604569"),
    ("Unknown_4864027116949045", "act_4864027116949045"),
    ("Unknown_1265947288969279", "act_1265947288969279"),
]

def make_key(name):
    # Convert name to a safe dictionary key
    k = re.sub(r'[^a-zA-Z0-9_]', '_', name).lower()
    k = re.sub(r'_+', '_', k)
    return k.strip('_')

# Update config.yaml
with open('config.yaml', 'r', encoding='utf-8') as f:
    config = yaml.safe_load(f)

config['clients'] = {}
for name, act_id in clients:
    key = make_key(name)
    config['clients'][key] = {
        'name': name,
        'act_id': act_id,
        'lead_action_types': [
            'lead',
            'onsite_conversion.lead_grouped',
            'onsite_conversion.messaging_first_reply'
        ]
    }

with open('config.yaml', 'w', encoding='utf-8') as f:
    yaml.dump(config, f, allow_unicode=True, sort_keys=False)

# Update index.html
html_path = 'docs/index.html'
with open(html_path, 'r', encoding='utf-8') as f:
    html = f.read()

# Build options string
options = []
# Add 'all' option first
options.append('<option value="all">Все кабинеты сразу</option>')
for name, act_id in clients:
    key = make_key(name)
    options.append(f'<option value="{key}">{name}</option>')

options_str = '\n            '.join(options)

# Replace everything between <select id="client"> and </select>
new_html = re.sub(
    r'(<select id="client">).*?(</select>)',
    f'\\1\n            {options_str}\n          \\2',
    html,
    flags=re.DOTALL
)

with open(html_path, 'w', encoding='utf-8') as f:
    f.write(new_html)

print("Updated config.yaml and docs/index.html successfully.")
