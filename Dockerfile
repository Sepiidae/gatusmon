FROM python

ADD app.py app.py
ADD config.json config.json
ADD startProd startProd
ADD requirements.txt requirements.txt

COPY templates templates

RUN chmod +x startProd

RUN python -m venv .venv
RUN source .venv/bin/activate
RUN pip install -r requirements.txt

CMD [ "./startProd" ]