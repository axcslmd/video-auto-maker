# 贡献指南

感谢参与项目。提交 Issue 前请删除客户数据、密钥、未公开素材和运行输出；提交代码时请保持宿主 Agent 无关的 `BASICROUTER_*` 公共协议。

## Pull Request 要求

1. 说明修改目的、影响范围和验证方式。
2. 新增第三方代码、素材或依赖时，列明准确来源、版本、许可证和原始版权声明。
3. 不得提交来源不明的 vendor 代码、客户素材、访问凭证或未经许可的二进制。
4. 运行相关测试和 `python3 scripts/license_compliance.py check`。
5. 在 Pull Request 描述中加入：

   > I have read and agree to CLA.md, and I have the right to submit this contribution.

未满足 CLA 或来源披露要求的贡献不会合并。贡献被接受不代表获得项目名称、商标或商业使用授权。
