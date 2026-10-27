# Local WeChat packages

Put a WeChat `.deb` here (or point the `wechat_vendor` build context at a directory holding
one, as LoAgent's compose file does) to install it instead of downloading from Tencent.
The file is looked up as `WeChatLinux_<x86_64|arm64>.deb`, or by the `WECHAT_DEB_FILE`
build argument. Its sha256 must match the pinned value in the Dockerfile either way.

`*.deb` files in this directory are ignored by git.
