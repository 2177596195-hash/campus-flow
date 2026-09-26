package edu.ylu.campus;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;

/**
 * CampusFlow 校园业务服务启动入口。
 *
 * <p>本服务是校园业务数据的唯一访问方：Python 侧 Agent 通过 HTTP 调用这里的 REST 接口，
 * 由这里的分层（Controller → Service → Repository）落到 MySQL。
 * 因此「谁知道 SQL」这件事被收敛在本模块内，Agent 侧不出现任何数据库依赖。
 */
@SpringBootApplication
public class CampusServiceApplication {

    public static void main(String[] args) {
        SpringApplication.run(CampusServiceApplication.class, args);
    }
}
