# ⚡ Crypto Sentinel Bot

![Python](https://img.shields.io/badge/python-3.9+-yellow.svg)
![Market](https://img.shields.io/badge/market-crypto-orange.svg)
![Status](https://img.shields.io/badge/status-active-brightgreen.svg)

Monitor inteligente de criptoactivos que utiliza **Análisis Técnico Institucional** para detectar oportunidades de entrada en tiempo real. Diseñado para operar en un mercado 24/7 sin interrupciones mediante GitHub Actions.

## 🚀 Estrategia de Trading (Cripto)
Debido a la volatilidad del mercado cripto, el bot utiliza filtros específicos para evitar "falsos breaks":

* **Filtro de Tendencia:** Solo opera si el precio está por encima de la **EMA 200** diaria.
* **Confirmación de Momentum:** Utiliza **RSI** (45-65) y **ADX > 20** para asegurar que el movimiento tiene respaldo de volumen.
* **Cruce de Medias:** Detecta el cruce de la **EMA 20** como señal de entrada rápida.
* **Gestión de Riesgo:** Ratio R:R mínimo de 2.0 calculado dinámicamente con el **ATR** (Average True Range).

## 🛠️ Configuración del Sistema

### Variables de Entorno (GitHub Secrets)
Para que el bot funcione, debes configurar los siguientes **Secrets** en tu repositorio:

| Secreto | Descripción |
| :--- | :--- |
| `TELEGRAM_BOT_TOKEN` | Token de API de tu bot de Telegram. |
| `TELEGRAM_CHAT_ID` | ID del chat donde recibirás las alertas. |

### 📦 Instalación Local
```bash
git clone [https://github.com/TU_USUARIO/TU_REPO_CRYPTO.git](https://github.com/TU_USUARIO/TU_REPO_CRYPTO.git)
cd TU_REPO_CRYPTO
pip install -r requirements.txt
python alert.py
```
### 🤝 Contribuciones
¡Las ideas para nuevos indicadores son bienvenidas! Para colaborar:

Crea una rama (git checkout -b feature/NuevoIndicador).

Realiza tus cambios y asegúrate de que el archivo crypto_state.json esté en el .gitignore.

Envía un Pull Request.

### ⚖️ Licencia
Distribuido bajo la Licencia MIT. El uso de este software es para fines informativos; el trading de criptomonedas implica un alto riesgo financiero.
