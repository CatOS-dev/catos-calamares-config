pkgname=catos-calamares-config
pkgver=26.05
pkgrel=2
pkgdesc="calamares for CatOS"
arch=('any')
url="https://github.com/Aromatic05/catos-calamares-config"
license=('GPL-3.0-or-later')
depends=('catos-branding' 'catos-calamares' 'python-yaml')
makedepends=('git')
provides=("$pkgname")
conflicts=('alci-calamares-config'
           'alci-calamares-config-dev'
           'alci-calamares-config-pure'
           'alci-calamares-config-btrfs')

source=("$pkgname::git+file://$PWD")
sha256sums=('SKIP')

package() {
    cd "$srcdir/$pkgname"
    
    # 安装calamares配置文件
    install -d "$pkgdir/etc/calamares"
    install -d "$pkgdir/usr/share/calamares"
    
    # 复制配置文件（根据实际文件结构调整）
    if [ -d "etc" ]; then
        cp -r etc/* "$pkgdir/etc/"
    fi
    
    if [ -d "usr" ]; then
        cp -r usr/* "$pkgdir/usr/"
    fi
}
