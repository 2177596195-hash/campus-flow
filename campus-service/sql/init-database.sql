-- 一次性初始化脚本：创建库（表结构由应用启动时的 schema.sql / data.sql 负责）
--
-- 用法（在仓库根目录执行）：
--   mysql -u root -p < campus-service/sql/init-database.sql
--
-- 之所以单独放库创建语句：JDBC 连接串必须先指定已存在的库，
-- 因此「建库」无法放进 Spring Boot 启动流程里。

CREATE DATABASE IF NOT EXISTS campus
    DEFAULT CHARACTER SET utf8mb4
    COLLATE utf8mb4_general_ci;

-- 测试库：Spring Boot 集成测试使用（ddl-auto=create-drop，用完即删表）
CREATE DATABASE IF NOT EXISTS campus_test
    DEFAULT CHARACTER SET utf8mb4
    COLLATE utf8mb4_general_ci;
