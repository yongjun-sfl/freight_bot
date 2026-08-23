CREATE TABLE IF NOT EXISTS driver_profiles (
    user_id BIGINT PRIMARY KEY,
    driver_name VARCHAR(128) NULL,
    home_yard ENUM('YARD_200', 'SDS_WH') NOT NULL DEFAULT 'YARD_200',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS shuttle_legs (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id BIGINT NOT NULL,
    trailer_number VARCHAR(64),
    bol_number VARCHAR(64) NULL,
    load_status ENUM('EMPTY', 'LOADED') DEFAULT 'LOADED',
    origin_location VARCHAR(128) DEFAULT 'Origin',
    destination_location VARCHAR(128) DEFAULT 'Destination',
    departure_time DATETIME NULL,
    arrival_time DATETIME NULL,
    arrival_action VARCHAR(64) NULL,
    dock_number VARCHAR(32) NULL,
    shipper_signed BOOLEAN DEFAULT FALSE,
    receiver_signed BOOLEAN DEFAULT FALSE,
    is_positioning_leg BOOLEAN DEFAULT FALSE,
    is_bobtail BOOLEAN DEFAULT FALSE,
    leg_status ENUM('IN_TRANSIT', 'COMPLETED') DEFAULT 'IN_TRANSIT',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_user_status (user_id, leg_status),
    INDEX idx_bol (bol_number)
);