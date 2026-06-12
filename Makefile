.PHONY: up down logs build test migrate shell

up:
	docker compose up -d

down:
	docker compose down

build:
	docker compose build

logs:
	docker compose logs -f

test:
	docker compose run --rm web pytest tests/ -v

migrate:
	docker compose run --rm web alembic upgrade head

shell:
	docker compose run --rm web python

lint:
	docker compose run --rm web python -m py_compile app/**/*.py
