FROM python:3.11-slim

WORKDIR /app

# RUN apt-get update && \
#     apt-get install -y --no-install-recommends tzdata && \
#     ln -sf /usr/share/zoneinfo/Asia/Tokyo /etc/localtime && \
#     echo "Asia/Tokyo" > /etc/timezone && \
#     dpkg-reconfigure -f noninteractive tzdata && \
#     apt-get clean && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# COPY fonts /fonts

COPY . .

CMD ["python", "bot.py"]