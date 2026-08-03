-- 由 DBA 在部署依赖 Cookie 刷新状态的应用版本前执行。
ALTER TABLE tb_medium_account
    ADD COLUMN cookie_version BIGINT NOT NULL DEFAULT 0 COMMENT 'Cookie 版本号',
    ADD COLUMN refresh_fence BIGINT NOT NULL DEFAULT 0 COMMENT '刷新围栏号',
    ADD COLUMN refresh_result VARCHAR(32) NOT NULL DEFAULT 'ready' COMMENT '刷新结果',
    ADD COLUMN refresh_code VARCHAR(32) NULL COMMENT '刷新失败码';
