DROP TABLE IF EXISTS gaap_metrics;
DROP TABLE IF EXISTS dws_gaap_di;

CREATE TABLE dws_gaap_di (
    ds DATE NOT NULL,
    product TEXT NOT NULL,
    region TEXT NOT NULL,
    user_type TEXT NOT NULL,
    user_id TEXT PRIMARY KEY,
    gaap_val NUMERIC(18, 2) NOT NULL,
    ytd_gaap NUMERIC(18, 2) NOT NULL
);

CREATE INDEX idx_dws_gaap_di_product_date ON dws_gaap_di (product, ds);
CREATE INDEX idx_dws_gaap_di_valid_user ON dws_gaap_di (product, ytd_gaap);
