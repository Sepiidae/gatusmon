FROM python

COPY app.py app.py
COPY config.json config.json
COPY startProd startProd
COPY requirements.txt requirements.txt
COPY templates /app/templates
WORKDIR /app


RUN chmod +x startProd

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --no-cache-dir -r requirements.txt

CMD [ "./startProd" ]