CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT, city TEXT, joined DATE);
CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER, ordered DATE, total REAL);
INSERT INTO customers VALUES (1, 'Ada', 'London', '2024-01-15'), (2, 'Grace', 'New York', '2024-03-02'), (3, 'Linus', 'Helsinki', '2024-06-20'), (4, 'Margaret', 'Boston', '2025-02-11');
INSERT INTO orders VALUES (1, 1, '2025-01-05', 42.50), (2, 1, '2025-02-10', 87.00), (3, 2, '2025-02-14', 19.99), (4, 3, '2025-03-01', 150.00), (5, 2, '2025-03-03', 64.25), (6, 4, '2025-03-20', 12.00);
