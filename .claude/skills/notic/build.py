#!/usr/bin/env python3
"""Construit notice_final.odt depuis <work_dir>/notice.html.

Usage: python3 build.py <work_dir> [src_img_dir]

Etapes:
  1. redimensionne les photos de src_img_dir (defaut: ./img) vers <work_dir>/img
  2. convertit notice.html en ODT Writer (writer8, pas writerweb8)
  3. integre les images liees dans le zip ODT (Pictures/ + manifest)
"""
import os
import re
import subprocess
import sys
import zipfile

from PIL import Image, ImageOps

MAX_PX = 900
QUALITY = 82


def resize(src_dir, dst_dir):
    os.makedirs(dst_dir, exist_ok=True)
    n = 0
    for name in sorted(os.listdir(src_dir)):
        if not name.lower().endswith((".jpg", ".jpeg", ".png")):
            continue
        im = ImageOps.exif_transpose(Image.open(os.path.join(src_dir, name)))
        im.thumbnail((MAX_PX, MAX_PX))
        im.save(os.path.join(dst_dir, name), quality=QUALITY)
        n += 1
    return n


def convert(work):
    out = os.path.join(work, "out")
    subprocess.run(
        ["soffice", "--headless", "--convert-to", "odt:writer8",
         "--infilter=HTML (StarWriter)", "notice.html", "--outdir", "out"],
        cwd=work, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return os.path.join(out, "notice.odt")


def embed(odt, img_dir, dst):
    zin = zipfile.ZipFile(odt)
    items = {n: zin.read(n) for n in zin.namelist()}
    content = items["content.xml"].decode("utf-8")
    manifest = items["META-INF/manifest.xml"].decode("utf-8")

    pics = {}
    for link in sorted(set(re.findall(r'xlink:href="([^"]*\.(?:jpg|jpeg|png))"', content))):
        base = os.path.basename(link)
        target = "Pictures/" + base
        with open(os.path.join(img_dir, base), "rb") as fh:
            pics[target] = fh.read()
        content = content.replace('xlink:href="%s"' % link, 'xlink:href="%s"' % target)

    media = lambda t: "image/png" if t.lower().endswith(".png") else "image/jpeg"
    entries = "".join(
        '<manifest:file-entry manifest:full-path="%s" manifest:media-type="%s"/>' % (t, media(t))
        for t in pics
    )
    manifest = manifest.replace("</manifest:manifest>", entries + "</manifest:manifest>")

    zout = zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED)
    zout.writestr("mimetype", "application/vnd.oasis.opendocument.text", zipfile.ZIP_STORED)
    for name, data in items.items():
        if name == "mimetype":
            continue
        if name == "content.xml":
            data = content.encode("utf-8")
        if name == "META-INF/manifest.xml":
            data = manifest.encode("utf-8")
        zout.writestr(name, data)
    for target, data in pics.items():
        zout.writestr(target, data)
    zout.close()
    return len(pics)


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    work = os.path.abspath(sys.argv[1])
    src_img = os.path.abspath(sys.argv[2]) if len(sys.argv) > 2 else os.path.abspath("img")

    html = os.path.join(work, "notice.html")
    if not os.path.exists(html):
        sys.exit("manque %s" % html)

    work_img = os.path.join(work, "img")
    print("%d photos redimensionnees" % resize(src_img, work_img))
    odt = convert(work)
    dst = os.path.join(work, "notice_final.odt")
    print("%d images integrees" % embed(odt, work_img, dst))
    print(dst)


if __name__ == "__main__":
    main()
