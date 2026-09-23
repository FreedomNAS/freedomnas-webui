# Maintainer: FreedomNAS Team <dev@freedomnas.io>
pkgname=freedomnas-webui
pkgver=1.0
pkgrel=1
pkgdesc="FreedomNAS WebUI — FastAPI appliance interface"
arch=('any')
url="https://codeberg.org/FreedomNAS/freedomnas-webui"
license=('MPL-2.0')
depends=(
    'freedomnas-core'
    'freedomnas-profiles'
    'python'
    'python-fastapi'
    'uvicorn'
    'python-websockets'
    'python-jinja'
    'python-pam'
    'python-yaml'
)
optdepends=('cronie: start the WebUI at boot through the @reboot entry')
source=("git+https://codeberg.org/FreedomNAS/freedomnas-webui.git#tag=v${pkgver}")
sha256sums=('SKIP')

package() {
    cd "${srcdir}/${pkgname}"

    install -d "${pkgdir}/usr/lib/freedomnas/webui"
    cp -r api frontend websocket "${pkgdir}/usr/lib/freedomnas/webui/"

    # firstboot.sh and supervisor.sh are shared verbatim with the image
    # builder and live in freedomnas-profiles/assets: symlink rather than
    # vendor a second copy that could drift.
    install -d "${pkgdir}/usr/lib/freedomnas/webui/bin"
    ln -sf ../../profiles/assets/firstboot.sh \
        "${pkgdir}/usr/lib/freedomnas/webui/bin/firstboot.sh"
    ln -sf ../../profiles/assets/supervisor.sh \
        "${pkgdir}/usr/lib/freedomnas/webui/bin/supervisor.sh"

    [ -f LICENSE ] && install -Dm 644 LICENSE "${pkgdir}/usr/share/licenses/${pkgname}/LICENSE"
    [ -f README.md ] && install -Dm 644 README.md "${pkgdir}/usr/share/doc/${pkgname}/README.md"
}
