<?php
/**
 * Read endpoint for the scraped property data.
 *
 * Query params (all optional):
 *   property_type      string  e.g. "Căn hộ chung cư"
 *   listing_type       string  "Bán" or "Cho thuê"
 *   location_city      string
 *   location_province  string
 *   min_price          float   VND
 *   max_price          float   VND
 *   min_acreage        float   m^2
 *   bedrooms           int
 *   q                  string  match on title/description/address
 *   page               int     1-based
 *   per_page           int     default 50, max 200
 */

header('Content-Type: application/json; charset=utf-8');

$config = [
    'host' => getenv('DB_HOST') ?: 'localhost',
    'port' => getenv('DB_PORT') ?: '3306',
    'name' => getenv('DB_NAME') ?: 'property',
    'user' => getenv('DB_USER') ?: 'root',
    'pass' => getenv('DB_PASSWORD') ?: '',
];

try {
    $pdo = new PDO(
        "mysql:host={$config['host']};port={$config['port']};dbname={$config['name']};charset=utf8mb4",
        $config['user'],
        $config['pass'],
        [
            PDO::ATTR_ERRMODE            => PDO::ERRMODE_EXCEPTION,
            PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
            PDO::ATTR_EMULATE_PREPARES   => false,
        ]
    );
} catch (Throwable $e) {
    http_response_code(500);
    echo json_encode(['error' => 'db_connect_failed']);
    exit;
}

$where  = [];
$params = [];

if (!empty($_GET['property_type'])) {
    $where[] = 'property_type = :property_type';
    $params[':property_type'] = $_GET['property_type'];
}
if (!empty($_GET['listing_type'])) {
    $where[] = 'listing_type = :listing_type';
    $params[':listing_type'] = $_GET['listing_type'];
}
if (!empty($_GET['location_city'])) {
    $where[] = 'location_city = :location_city';
    $params[':location_city'] = $_GET['location_city'];
}
if (!empty($_GET['location_province'])) {
    $where[] = 'location_province = :location_province';
    $params[':location_province'] = $_GET['location_province'];
}
if (isset($_GET['min_price']) && $_GET['min_price'] !== '') {
    $where[] = 'price >= :min_price';
    $params[':min_price'] = (float) $_GET['min_price'];
}
if (isset($_GET['max_price']) && $_GET['max_price'] !== '') {
    $where[] = 'price <= :max_price';
    $params[':max_price'] = (float) $_GET['max_price'];
}
if (isset($_GET['min_acreage']) && $_GET['min_acreage'] !== '') {
    $where[] = 'acreage >= :min_acreage';
    $params[':min_acreage'] = (float) $_GET['min_acreage'];
}
if (isset($_GET['bedrooms']) && $_GET['bedrooms'] !== '') {
    $where[] = 'bedrooms = :bedrooms';
    $params[':bedrooms'] = (int) $_GET['bedrooms'];
}
if (!empty($_GET['q'])) {
    $where[] = '(title LIKE :q OR description LIKE :q OR address LIKE :q)';
    $params[':q'] = '%' . $_GET['q'] . '%';
}

$page     = max(1, (int) ($_GET['page'] ?? 1));
$perPage  = min(200, max(1, (int) ($_GET['per_page'] ?? 50)));
$offset   = ($page - 1) * $perPage;

$whereSql = $where ? 'WHERE ' . implode(' AND ', $where) : '';

$countStmt = $pdo->prepare("SELECT COUNT(*) AS n FROM scrapy $whereSql");
$countStmt->execute($params);
$total = (int) $countStmt->fetch()['n'];

$sql = "SELECT url_source, listing_code, title, slug, description, footer_disclaimer,
               price, acreage, bedrooms, bathrooms,
               property_type, legal_type, news_type, interior,
               date_posted, posted_by, hidden_number, phone_number,
               agent_id, agent_url,
               location_city, location_province, listing_type,
               thumbnail_paths, large_image_paths, created_at
        FROM scrapy
        $whereSql
        ORDER BY created_at DESC
        LIMIT :limit OFFSET :offset";

$stmt = $pdo->prepare($sql);
foreach ($params as $k => $v) {
    $stmt->bindValue($k, $v);
}
$stmt->bindValue(':limit',  $perPage, PDO::PARAM_INT);
$stmt->bindValue(':offset', $offset,  PDO::PARAM_INT);
$stmt->execute();

$rows = array_map(function ($r) {
    foreach (['thumbnail_paths', 'large_image_paths'] as $col) {
        if (!empty($r[$col])) {
            $r[$col] = json_decode($r[$col], true);
        }
    }
    return $r;
}, $stmt->fetchAll());

echo json_encode([
    'total'    => $total,
    'page'     => $page,
    'per_page' => $perPage,
    'data'     => $rows,
], JSON_UNESCAPED_UNICODE | JSON_PRETTY_PRINT);
