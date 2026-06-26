"""
================================================================
GIS.ight — Auto Refresh GEE Tile URLs
================================================================
Script ini:
1. Login ke Google Earth Engine via Service Account
2. Hitung ulang semua layer proyek
3. Dapatkan tile URL baru (valid 2 jam sejak dijalankan)
4. Update file data/projects.json di GitHub secara otomatis

Dijalankan otomatis setiap hari via GitHub Actions
================================================================
"""

import ee
import json
import os
import base64
import requests
from datetime import datetime

# ================================================================
# KONFIGURASI — sesuaikan bagian ini
# ================================================================

# GitHub repo kamu
GITHUB_OWNER = "uronjm"
GITHUB_REPO  = "Gis.ight"
GITHUB_FILE  = "data/projects.json"       # path file di repo
GITHUB_TOKEN = os.environ.get("GH_TOKEN") # dari GitHub Secrets

# GEE Service Account
GEE_SERVICE_ACCOUNT = os.environ.get("GEE_SERVICE_ACCOUNT")  # email SA
GEE_KEY_JSON        = os.environ.get("GEE_KEY_JSON")         # isi file JSON key

# Asset GEE
ASSET_OKI = 'projects/wijaya-474816/assets/OKI'

# ================================================================
# INISIALISASI GEE
# ================================================================
def init_gee():
    print("🔐 Menginisialisasi Google Earth Engine...")
    key_data = json.loads(GEE_KEY_JSON)
    credentials = ee.ServiceAccountCredentials(
        email=GEE_SERVICE_ACCOUNT,
        key_data=json.dumps(key_data)
    )
    ee.Initialize(credentials)
    print("✅ GEE berhasil diinisialisasi")

# ================================================================
# HITUNG SEMUA LAYER
# ================================================================
def compute_layers():
    print("⚙️  Menghitung layer...")

    roi  = ee.FeatureCollection(ASSET_OKI)
    area = roi.geometry()

    start_year = 2015
    end_year   = 2024

    # NDVI
    ndvi_col = (ee.ImageCollection('MODIS/006/MOD13Q1')
        .filterBounds(area)
        .filterDate(f'{start_year}-01-01', f'{end_year}-12-31')
        .select('NDVI')
        .map(lambda img: img.multiply(0.0001)
             .copyProperties(img, ['system:time_start'])))

    ndvi_mean = ndvi_col.mean().clip(area)
    ndvi_min  = ndvi_col.min()
    ndvi_max  = ndvi_col.max()

    # NDVI Anomaly
    ndvi_anom_mean = (ndvi_col
        .map(lambda img: img.subtract(ndvi_mean)
             .rename('NDVI_Anomaly')
             .copyProperties(img, ['system:time_start']))
        .mean().clip(area))

    # VCI
    vci_mean = (ndvi_col
        .map(lambda img: img.subtract(ndvi_min)
             .divide(ndvi_max.subtract(ndvi_min))
             .multiply(100).rename('VCI')
             .copyProperties(img, ['system:time_start']))
        .mean().clip(area))

    # CHIRPS + SPI
    rain_daily = (ee.ImageCollection('UCSB-CHG/CHIRPS/DAILY')
        .filterBounds(area)
        .filterDate(f'{start_year}-01-01', f'{end_year}-12-31'))

    def make_monthly(y):
        def make_month(m):
            start = ee.Date.fromYMD(y, m, 1)
            end   = start.advance(1, 'month')
            return (rain_daily.filterDate(start, end).sum()
                    .rename('Rain')
                    .set('system:time_start', start.millis()))
        return ee.List.sequence(1, 12).map(make_month)

    monthly_rain = ee.ImageCollection(
        ee.List.sequence(start_year, end_year).map(make_monthly).flatten()
    )

    rain_mean = monthly_rain.mean().clip(area)
    rain_std  = monthly_rain.reduce(ee.Reducer.stdDev()).clip(area)
    spi_mean  = (monthly_rain
        .map(lambda img: img.subtract(rain_mean).divide(rain_std)
             .rename('SPI').copyProperties(img, ['system:time_start']))
        .mean().clip(area))

    # Klasifikasi
    ndvi_class = (ndvi_anom_mean
        .expression("(b('NDVI_Anomaly')<=-0.15)?3:(b('NDVI_Anomaly')<=-0.05)?2:(b('NDVI_Anomaly')<=0.05)?1:0")
        .rename('NDVI_Class').clip(area))

    vci_class = (vci_mean
        .expression("(b('VCI')<35)?3:(b('VCI')<50)?2:(b('VCI')<65)?1:0")
        .rename('VCI_Class').clip(area))

    spi_class = (spi_mean
        .expression("(b('SPI')<=-2.0)?3:(b('SPI')<=-1.5)?2:(b('SPI')<=-1.0)?1:0")
        .rename('SPI_Class').clip(area))

    drought_index = (ndvi_class.multiply(0.4)
        .add(vci_class.multiply(0.3))
        .add(spi_class.multiply(0.3))
        .rename('Drought_Index'))

    drought_class = (drought_index
        .expression("(b('Drought_Index')>=2.5)?4:(b('Drought_Index')>=1.8)?3:(b('Drought_Index')>=1.0)?2:1")
        .rename('Drought_Class').clip(area))

    # LST MODIS
    lst = (ee.ImageCollection('MODIS/061/MOD11A2')
        .filterBounds(area)
        .filterDate('2015-01-01', '2025-12-31')
        .select('LST_Day_1km')
        .map(lambda img: img.multiply(0.02).subtract(273.15)
             .copyProperties(img, ['system:time_start']))
        .mean().clip(area))

    # ================================================================
    # dNBR Burn Severity (Landsat 8, OKI)
    # Pre-fire: 2018 | Post-fire: 2020
    # ================================================================
    def prep_sr_l8(image):
        qa_mask  = image.select('QA_PIXEL').bitwiseAnd(int('11111', 2)).eq(0)
        sat_mask = image.select('QA_RADSAT').eq(0)
        scale_names  = ['REFLECTANCE_MULT_BAND_1','REFLECTANCE_MULT_BAND_2','REFLECTANCE_MULT_BAND_3',
                        'REFLECTANCE_MULT_BAND_4','REFLECTANCE_MULT_BAND_5','REFLECTANCE_MULT_BAND_6',
                        'REFLECTANCE_MULT_BAND_7']
        offset_names = ['REFLECTANCE_ADD_BAND_1','REFLECTANCE_ADD_BAND_2','REFLECTANCE_ADD_BAND_3',
                        'REFLECTANCE_ADD_BAND_4','REFLECTANCE_ADD_BAND_5','REFLECTANCE_ADD_BAND_6',
                        'REFLECTANCE_ADD_BAND_7']
        scale_img  = ee.Image.constant(image.toDictionary().select(scale_names).values())
        offset_img = ee.Image.constant(image.toDictionary().select(offset_names).values())
        scaled = (image.select(['SR_B1','SR_B2','SR_B3','SR_B4','SR_B5','SR_B6','SR_B7'])
                  .multiply(scale_img).add(offset_img))
        return (image.addBands(scaled, None, True)
                .updateMask(qa_mask).updateMask(sat_mask))

    def get_nbr(image):
        return image.normalizedDifference(['SR_B5','SR_B7']).rename('NBR')

    pre_fire  = (ee.ImageCollection('LANDSAT/LC08/C02/T1_L2')
        .filterBounds(area).filterDate('2018-01-01','2018-12-31')
        .map(prep_sr_l8).median())
    nbr_pre   = get_nbr(pre_fire).clip(area)

    post_fire = (ee.ImageCollection('LANDSAT/LC08/C02/T1_L2')
        .filterBounds(area).filterDate('2020-01-01','2020-12-31')
        .map(prep_sr_l8).median())
    nbr_post  = get_nbr(post_fire).clip(area)

    dnbr = nbr_pre.subtract(nbr_post).rename('dNBR').clip(area)

    burn_severity = (dnbr.expression(
        "(b('dNBR') < 0.1) ? 0 : (b('dNBR') < 0.27) ? 1 : (b('dNBR') < 0.44) ? 2 : 3"
    ).rename('Severity').clip(area))

    print("✅ Semua layer berhasil dihitung")

    return {
        # Kekeringan OKI
        'lst_modis':        (lst,            {'min':10,   'max':50,  'palette':['#00FF00','#FFFF00','#FFA500','#FF0000']}),
        'ndvi_mean_oki':    (ndvi_mean,      {'min':0,    'max':1,   'palette':['#8B4513','#FFFF00','#006400']}),
        'ndvi_anomaly_oki': (ndvi_anom_mean, {'min':-0.2, 'max':0.2, 'palette':['#FF0000','#FFFFFF','#008000']}),
        'ndvi_class_oki':   (ndvi_class,     {'min':0,    'max':3,   'palette':['#008000','#FFFF00','#FFA500','#FF0000']}),
        'vci_mean_oki':     (vci_mean,       {'min':0,    'max':100, 'palette':['#FF0000','#FFFF00','#008000']}),
        'vci_class_oki':    (vci_class,      {'min':0,    'max':3,   'palette':['#008000','#FFFF00','#FFA500','#FF0000']}),
        'rainfall_mean_oki':(rain_mean,      {'min':0,    'max':300, 'palette':['#FFFFFF','#00BFFF','#00008B']}),
        'spi_mean_oki':     (spi_mean,       {'min':-2,   'max':2,   'palette':['#FF0000','#FFFFFF','#0000FF']}),
        'spi_class_oki':    (spi_class,      {'min':0,    'max':3,   'palette':['#008000','#FFFF00','#FFA500','#FF0000']}),
        'drought_index_oki':(drought_index,  {'min':0,    'max':3,   'palette':['#008000','#FFFF00','#FFA500','#FF0000']}),
        'drought_class_oki':(drought_class,  {'min':1,    'max':4,   'palette':['#008000','#FFFF00','#FFA500','#FF0000']}),
        # dNBR Burn Severity OKI
        'nbr_pre_oki':      (nbr_pre,        {'min':-0.5, 'max':1,   'palette':['#8B4513','#FFFF00','#006400']}),
        'nbr_post_oki':     (nbr_post,       {'min':-0.5, 'max':1,   'palette':['#8B4513','#FFFF00','#006400']}),
        'dnbr_oki':         (dnbr,           {'min':-0.5, 'max':1,   'palette':['#006400','#FFFF00','#FFA500','#FF0000']}),
        'burn_severity_oki':(burn_severity,  {'min':0,    'max':3,   'palette':['#006400','#FFFF00','#FFA500','#FF0000']}),
    }

# ================================================================
# DAPATKAN TILE URL DARI GEE
# ================================================================
def get_tile_urls(layers):
    print("🔗 Mengambil tile URLs...")
    urls = {}
    for layer_id, (image, vis) in layers.items():
        try:
            map_id = ee.data.getMapId({'image': image, 'visParams': vis})
            url    = map_id['tile_fetcher'].url_format
            # Pastikan format {z}/{x}/{y} benar
            url = url.replace('%7Bz%7D','{z}').replace('%7Bx%7D','{x}').replace('%7By%7D','{y}')
            urls[layer_id] = url
            print(f"  ✅ {layer_id}")
        except Exception as e:
            print(f"  ❌ {layer_id} — {e}")
            urls[layer_id] = None
    return urls

# ================================================================
# BANGUN projects.json LENGKAP
# ================================================================
def build_projects_json(urls):
    now = datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')

    def url(key):
        return urls.get(key) or "URL_TIDAK_TERSEDIA"

    projects = [
        {
            "id": "lst_modis",
            "name": "Land Surface Temperature — MODIS",
            "desc": "Rata-rata LST 2015–2025 dari MODIS MOD11A2 resolusi 1km. Konversi ke Celsius.",
            "sensor": "MODIS MOD11A2", "tahun": "2015–2025",
            "lokasi": "Ogan Komering Ilir, Sumatera Selatan", "emoji": "🌡️",
            "tileUrl": url('lst_modis'),
            "bounds": [[-3.5, 104.0], [-2.0, 105.5]],
            "stats": {"min":10,"max":50,"mean":32,"std":8,"bands":1,"width":1000,"height":1000,"nodata":-9999},
            "legendType": "gradient",
            "legendPalette": ["#00FF00","#FFFF00","#FFA500","#FF0000"],
            "legendLabels": ["10°C","30°C","50°C"],
            "legendTitle": "Suhu Permukaan (°C)",
            "tags": ["LST","MODIS","Suhu","Thermal"],
            "lastRefresh": now
        },
        {
            "id": "ndvi_mean_oki",
            "name": "NDVI Mean — OKI 2015–2024",
            "desc": "Rata-rata NDVI dari MODIS MOD13Q1 periode 2015–2024. Menggambarkan kondisi kerapatan vegetasi rata-rata wilayah OKI.",
            "sensor": "MODIS MOD13Q1", "tahun": "2015–2024",
            "lokasi": "Ogan Komering Ilir, Sumatera Selatan", "emoji": "🌿",
            "tileUrl": url('ndvi_mean_oki'),
            "bounds": [[-4.5, 104.5], [-2.5, 106.5]],
            "stats": {"min":0,"max":1,"mean":0.5,"std":0.2,"bands":1,"width":1000,"height":1000,"nodata":-9999},
            "legendType": "gradient",
            "legendPalette": ["#8B4513","#FFFF00","#006400"],
            "legendLabels": ["0","0.5","1.0"],
            "legendTitle": "Nilai NDVI",
            "tags": ["NDVI","Vegetasi","MODIS","Kekeringan","OKI"],
            "lastRefresh": now
        },
        {
            "id": "ndvi_anomaly_oki",
            "name": "NDVI Anomali — OKI 2015–2024",
            "desc": "Anomali NDVI terhadap rata-rata jangka panjang. Nilai negatif (merah) mengindikasikan penurunan kondisi vegetasi.",
            "sensor": "MODIS MOD13Q1", "tahun": "2015–2024",
            "lokasi": "Ogan Komering Ilir, Sumatera Selatan", "emoji": "📉",
            "tileUrl": url('ndvi_anomaly_oki'),
            "bounds": [[-4.5, 104.5], [-2.5, 106.5]],
            "stats": {"min":-0.2,"max":0.2,"mean":0,"std":0.08,"bands":1,"width":1000,"height":1000,"nodata":-9999},
            "legendType": "gradient",
            "legendPalette": ["#FF0000","#FFFFFF","#008000"],
            "legendLabels": ["-0.2","0","+0.2"],
            "legendTitle": "Anomali NDVI",
            "tags": ["NDVI","Anomali","Kekeringan","OKI"],
            "lastRefresh": now
        },
        {
            "id": "ndvi_class_oki",
            "name": "Klasifikasi Anomali NDVI — OKI",
            "desc": "Klasifikasi kekeringan berbasis anomali NDVI: Normal, Ringan, Sedang, Berat.",
            "sensor": "MODIS MOD13Q1", "tahun": "2015–2024",
            "lokasi": "Ogan Komering Ilir, Sumatera Selatan", "emoji": "🗺️",
            "tileUrl": url('ndvi_class_oki'),
            "bounds": [[-4.5, 104.5], [-2.5, 106.5]],
            "stats": {"min":0,"max":3,"mean":1.5,"std":1,"bands":1,"width":1000,"height":1000,"nodata":-1},
            "legendType": "categorical",
            "legendItems": [
                {"color":"#008000","label":"Normal (0)"},
                {"color":"#FFFF00","label":"Ringan (1)"},
                {"color":"#FFA500","label":"Sedang (2)"},
                {"color":"#FF0000","label":"Berat (3)"}
            ],
            "legendTitle": "Kelas NDVI Anomali",
            "tags": ["NDVI","Klasifikasi","Kekeringan","OKI"],
            "lastRefresh": now
        },
        {
            "id": "vci_mean_oki",
            "name": "Vegetation Condition Index — OKI",
            "desc": "VCI mengukur kondisi vegetasi relatif terhadap nilai historis min dan maks. Nilai rendah (<35%) indikasi stres vegetasi.",
            "sensor": "MODIS MOD13Q1", "tahun": "2015–2024",
            "lokasi": "Ogan Komering Ilir, Sumatera Selatan", "emoji": "🌱",
            "tileUrl": url('vci_mean_oki'),
            "bounds": [[-4.5, 104.5], [-2.5, 106.5]],
            "stats": {"min":0,"max":100,"mean":50,"std":20,"bands":1,"width":1000,"height":1000,"nodata":-9999},
            "legendType": "gradient",
            "legendPalette": ["#FF0000","#FFFF00","#008000"],
            "legendLabels": ["0%","50%","100%"],
            "legendTitle": "VCI (%)",
            "tags": ["VCI","Vegetasi","Kekeringan","OKI"],
            "lastRefresh": now
        },
        {
            "id": "vci_class_oki",
            "name": "Klasifikasi VCI — OKI",
            "desc": "Klasifikasi kondisi vegetasi berbasis VCI: Normal (≥65%), Ringan (50–65%), Sedang (35–50%), Berat (<35%).",
            "sensor": "MODIS MOD13Q1", "tahun": "2015–2024",
            "lokasi": "Ogan Komering Ilir, Sumatera Selatan", "emoji": "🌾",
            "tileUrl": url('vci_class_oki'),
            "bounds": [[-4.5, 104.5], [-2.5, 106.5]],
            "stats": {"min":0,"max":3,"mean":1.5,"std":1,"bands":1,"width":1000,"height":1000,"nodata":-1},
            "legendType": "categorical",
            "legendItems": [
                {"color":"#008000","label":"Normal (VCI ≥ 65%)"},
                {"color":"#FFFF00","label":"Ringan (50–65%)"},
                {"color":"#FFA500","label":"Sedang (35–50%)"},
                {"color":"#FF0000","label":"Berat (< 35%)"}
            ],
            "legendTitle": "Kelas VCI",
            "tags": ["VCI","Klasifikasi","Kekeringan","OKI"],
            "lastRefresh": now
        },
        {
            "id": "rainfall_mean_oki",
            "name": "Curah Hujan Rata-rata — OKI",
            "desc": "Rata-rata curah hujan bulanan dari CHIRPS 2015–2024. Distribusi spasial curah hujan wilayah OKI.",
            "sensor": "CHIRPS Daily", "tahun": "2015–2024",
            "lokasi": "Ogan Komering Ilir, Sumatera Selatan", "emoji": "🌧️",
            "tileUrl": url('rainfall_mean_oki'),
            "bounds": [[-4.5, 104.5], [-2.5, 106.5]],
            "stats": {"min":0,"max":300,"mean":150,"std":80,"bands":1,"width":500,"height":500,"nodata":-9999},
            "legendType": "gradient",
            "legendPalette": ["#FFFFFF","#00BFFF","#00008B"],
            "legendLabels": ["0mm","150mm","300mm"],
            "legendTitle": "Curah Hujan (mm)",
            "tags": ["Curah Hujan","CHIRPS","Kekeringan","OKI"],
            "lastRefresh": now
        },
        {
            "id": "spi_mean_oki",
            "name": "Standardized Precipitation Index — OKI",
            "desc": "SPI mengkuantifikasi defisit atau surplus curah hujan relatif terhadap historis. Nilai ≤ -1 indikasi kekeringan meteorologis.",
            "sensor": "CHIRPS Daily", "tahun": "2015–2024",
            "lokasi": "Ogan Komering Ilir, Sumatera Selatan", "emoji": "💧",
            "tileUrl": url('spi_mean_oki'),
            "bounds": [[-4.5, 104.5], [-2.5, 106.5]],
            "stats": {"min":-2,"max":2,"mean":0,"std":0.8,"bands":1,"width":500,"height":500,"nodata":-9999},
            "legendType": "gradient",
            "legendPalette": ["#FF0000","#FFFFFF","#0000FF"],
            "legendLabels": ["-2 (Sangat Kering)","0 (Normal)","+2 (Sangat Basah)"],
            "legendTitle": "Nilai SPI",
            "tags": ["SPI","Curah Hujan","Kekeringan","OKI"],
            "lastRefresh": now
        },
        {
            "id": "spi_class_oki",
            "name": "Klasifikasi SPI — OKI",
            "desc": "Klasifikasi kekeringan meteorologis berbasis SPI. Semakin merah semakin parah defisit curah hujan.",
            "sensor": "CHIRPS Daily", "tahun": "2015–2024",
            "lokasi": "Ogan Komering Ilir, Sumatera Selatan", "emoji": "☔",
            "tileUrl": url('spi_class_oki'),
            "bounds": [[-4.5, 104.5], [-2.5, 106.5]],
            "stats": {"min":0,"max":3,"mean":1,"std":1,"bands":1,"width":500,"height":500,"nodata":-1},
            "legendType": "categorical",
            "legendItems": [
                {"color":"#008000","label":"Normal (SPI > -1.0)"},
                {"color":"#FFFF00","label":"Ringan (-1.0 s/d -1.5)"},
                {"color":"#FFA500","label":"Sedang (-1.5 s/d -2.0)"},
                {"color":"#FF0000","label":"Berat (≤ -2.0)"}
            ],
            "legendTitle": "Kelas SPI",
            "tags": ["SPI","Klasifikasi","Kekeringan","OKI"],
            "lastRefresh": now
        },
        {
            "id": "drought_index_oki",
            "name": "Indeks Kekeringan Komposit — OKI",
            "desc": "Indeks kekeringan gabungan dari NDVI Anomali (40%), VCI (30%), dan SPI (30%). Semakin tinggi nilai semakin parah kekeringan.",
            "sensor": "MODIS + CHIRPS", "tahun": "2015–2024",
            "lokasi": "Ogan Komering Ilir, Sumatera Selatan", "emoji": "📊",
            "tileUrl": url('drought_index_oki'),
            "bounds": [[-4.5, 104.5], [-2.5, 106.5]],
            "stats": {"min":0,"max":3,"mean":1.5,"std":0.8,"bands":1,"width":1000,"height":1000,"nodata":-1},
            "legendType": "gradient",
            "legendPalette": ["#008000","#FFFF00","#FFA500","#FF0000"],
            "legendLabels": ["0 (Tidak Kering)","1.5 (Sedang)","3 (Berat)"],
            "legendTitle": "Drought Index",
            "tags": ["Drought Index","Komposit","Kekeringan","OKI"],
            "lastRefresh": now
        },
        {
            "id": "drought_class_oki",
            "name": "Klasifikasi Kekeringan Final — OKI",
            "desc": "Klasifikasi kekeringan final 4 kelas berbasis indeks komposit NDVI+VCI+SPI. Hasil akhir analisis kekeringan OKI 2015–2024.",
            "sensor": "MODIS + CHIRPS", "tahun": "2015–2024",
            "lokasi": "Ogan Komering Ilir, Sumatera Selatan", "emoji": "🔥",
            "tileUrl": url('drought_class_oki'),
            "bounds": [[-4.5, 104.5], [-2.5, 106.5]],
            "stats": {"min":1,"max":4,"mean":2,"std":1,"bands":1,"width":1000,"height":1000,"nodata":-1},
            "legendType": "categorical",
            "legendItems": [
                {"color":"#008000","label":"Kelas 1 — Tidak Kering"},
                {"color":"#FFFF00","label":"Kelas 2 — Kekeringan Ringan"},
                {"color":"#FFA500","label":"Kelas 3 — Kekeringan Sedang"},
                {"color":"#FF0000","label":"Kelas 4 — Kekeringan Berat"}
            ],
            "legendTitle": "Kelas Kekeringan",
            "tags": ["Kekeringan","Klasifikasi Final","OKI","Sumatera Selatan"],
            "lastRefresh": now
        },
        # ── dNBR Burn Severity ─────────────────────────────────────
        {
            "id": "nbr_pre_oki",
            "name": "NBR Pre-Fire — OKI 2018",
            "desc": "Normalized Burn Ratio sebelum kebakaran (2018) dari Landsat 8. Nilai tinggi menunjukkan vegetasi sehat dan lebat.",
            "sensor": "Landsat 8 OLI", "tahun": "2018",
            "lokasi": "Ogan Komering Ilir, Sumatera Selatan", "emoji": "🌿",
            "tileUrl": url("nbr_pre_oki"),
            "bounds": [[-4.5, 104.5], [-2.5, 106.5]],
            "stats": {"min":-0.5,"max":1,"mean":0.4,"std":0.2,"bands":1,"width":2048,"height":2048,"nodata":-9999},
            "legendType": "gradient",
            "legendPalette": ["#8B4513","#FFFF00","#006400"],
            "legendLabels": ["-0.5","0.25","1.0"],
            "legendTitle": "Nilai NBR",
            "tags": ["NBR","Pre-Fire","Kebakaran","Landsat 8","OKI"],
            "lastRefresh": now
        },
        {
            "id": "nbr_post_oki",
            "name": "NBR Post-Fire — OKI 2020",
            "desc": "Normalized Burn Ratio setelah kebakaran (2020) dari Landsat 8. Nilai rendah (coklat) menunjukkan area yang terdampak kebakaran.",
            "sensor": "Landsat 8 OLI", "tahun": "2020",
            "lokasi": "Ogan Komering Ilir, Sumatera Selatan", "emoji": "🔥",
            "tileUrl": url("nbr_post_oki"),
            "bounds": [[-4.5, 104.5], [-2.5, 106.5]],
            "stats": {"min":-0.5,"max":1,"mean":0.2,"std":0.25,"bands":1,"width":2048,"height":2048,"nodata":-9999},
            "legendType": "gradient",
            "legendPalette": ["#8B4513","#FFFF00","#006400"],
            "legendLabels": ["-0.5","0.25","1.0"],
            "legendTitle": "Nilai NBR",
            "tags": ["NBR","Post-Fire","Kebakaran","Landsat 8","OKI"],
            "lastRefresh": now
        },
        {
            "id": "dnbr_oki",
            "name": "dNBR — Burn Index OKI 2018–2020",
            "desc": "Differenced NBR (dNBR = NBR Pre - NBR Post) menggambarkan perubahan kondisi vegetasi akibat kebakaran. Nilai positif tinggi menunjukkan area terbakar parah.",
            "sensor": "Landsat 8 OLI", "tahun": "2018–2020",
            "lokasi": "Ogan Komering Ilir, Sumatera Selatan", "emoji": "📉",
            "tileUrl": url("dnbr_oki"),
            "bounds": [[-4.5, 104.5], [-2.5, 106.5]],
            "stats": {"min":-0.5,"max":1,"mean":0.2,"std":0.3,"bands":1,"width":2048,"height":2048,"nodata":-9999},
            "legendType": "gradient",
            "legendPalette": ["#006400","#FFFF00","#FFA500","#FF0000"],
            "legendLabels": ["-0.5 (Regrowth)","0.25","1.0 (Terbakar Parah)"],
            "legendTitle": "Nilai dNBR",
            "tags": ["dNBR","Kebakaran","Burn Index","Landsat 8","OKI"],
            "lastRefresh": now
        },
        {
            "id": "burn_severity_oki",
            "name": "Burn Severity Classification — OKI",
            "desc": "Klasifikasi tingkat keparahan kebakaran berbasis dNBR: Tidak Terbakar, Rendah, Sedang, Tinggi. Analisis dampak kebakaran OKI 2018–2020.",
            "sensor": "Landsat 8 OLI", "tahun": "2018–2020",
            "lokasi": "Ogan Komering Ilir, Sumatera Selatan", "emoji": "🗺️",
            "tileUrl": url("burn_severity_oki"),
            "bounds": [[-4.5, 104.5], [-2.5, 106.5]],
            "stats": {"min":0,"max":3,"mean":1.5,"std":1,"bands":1,"width":2048,"height":2048,"nodata":-1},
            "legendType": "categorical",
            "legendItems": [
                {"color": "#006400", "label": "Tidak Terbakar (dNBR < 0.1)"},
                {"color": "#FFFF00", "label": "Rendah (0.1–0.27)"},
                {"color": "#FFA500", "label": "Sedang (0.27–0.44)"},
                {"color": "#FF0000", "label": "Tinggi (> 0.44)"}
            ],
            "legendTitle": "Kelas Burn Severity",
            "tags": ["Burn Severity","dNBR","Kebakaran","Klasifikasi","OKI"],
            "lastRefresh": now
        }
    ]

    return projects

# ================================================================
# UPDATE projects.json DI GITHUB
# ================================================================
def update_github(projects):
    print("📤 Mengupdate projects.json di GitHub...")

    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json"
    }

    # Ambil SHA file saat ini (diperlukan untuk update)
    api_url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{GITHUB_FILE}"
    res = requests.get(api_url, headers=headers)

    if res.status_code == 200:
        sha = res.json()['sha']
    else:
        print(f"  ⚠️  File belum ada, akan dibuat baru (status: {res.status_code})")
        sha = None

    # Encode konten ke base64
    content_str  = json.dumps(projects, indent=2, ensure_ascii=False)
    content_b64  = base64.b64encode(content_str.encode('utf-8')).decode('utf-8')

    # Commit message dengan timestamp
    now     = datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')
    payload = {
        "message": f"🔄 Auto-refresh GEE tile URLs — {now}",
        "content": content_b64,
        "branch":  "main"
    }
    if sha:
        payload["sha"] = sha

    res2 = requests.put(api_url, headers=headers, json=payload)

    if res2.status_code in [200, 201]:
        print(f"✅ projects.json berhasil diupdate di GitHub!")
        print(f"   Commit: {res2.json()['commit']['sha'][:7]}")
    else:
        print(f"❌ Gagal update GitHub: {res2.status_code}")
        print(res2.text)

# ================================================================
# MAIN
# ================================================================
if __name__ == "__main__":
    print("=" * 60)
    print("  GIS.ight — Auto Refresh GEE Tile URLs")
    print(f"  {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 60)

    init_gee()
    layers   = compute_layers()
    urls     = get_tile_urls(layers)
    projects = build_projects_json(urls)
    update_github(projects)

    print("\n🎉 Selesai! WebGIS akan terupdate dalam ~1 menit.")
