FROM python

ADD app.py app.py
ADD config.json config.json
ADD startProd startProd
ADD requirements.txt requirements.txt

COPY templates templates

RUN chmod +x startProd

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --no-cache-dir -r requirements.txt

CMD [ "./startProd" ]