CREATE TABLE IF NOT EXISTS drivers (
    user_id BIGINT PRIMARY KEY,
    driver_name VARCHAR(100) NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS messages (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id BIGINT NOT NULL,
    original_text TEXT,
    origin VARCHAR(100) DEFAULT NULL,
    destination VARCHAR(100) DEFAULT NULL,
    departure_time VARCHAR(100) DEFAULT NULL,
    arrival_time VARCHAR(100) DEFAULT NULL,
    bol_number VARCHAR(100) DEFAULT NULL,
    trailer_number VARCHAR(100) DEFAULT NULL,
    shipper_signed TINYINT(1) DEFAULT 0,
    receiver_signed TINYINT(1) DEFAULT 0,
    image_blob LONGBLOB DEFAULT NULL,
    status VARCHAR(50) DEFAULT 'active',
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
