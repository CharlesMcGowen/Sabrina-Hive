"""Read-only model telemetry routes. Importing this server cannot dispatch chat work."""
import argparse
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def create_app(*,port=8096):
    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import FileResponse,JSONResponse
    from tools import model_infrastructure_view,wsp_research_view
    app=FastAPI(title='Sabrina Model Telemetry')
    app.add_middleware(CORSMiddleware,allow_origins=['null',f'http://127.0.0.1:{port}',f'http://localhost:{port}'],
        allow_methods=['GET'],allow_headers=['Content-Type'])

    @app.get('/health')
    def health():return {'service':'model_telemetry','read_only':True,'port':port}

    @app.get('/api/wsp-research')
    def research():return JSONResponse(wsp_research_view.snapshot(),headers={'Cache-Control':'no-store'})

    @app.get('/api/model-infrastructure')
    def infrastructure():return JSONResponse(model_infrastructure_view.snapshot(),headers={'Cache-Control':'no-store'})

    @app.get('/')
    @app.get('/sabrina_control_center.html')
    def console():return FileResponse(ROOT/'sabrina_control_center.html',headers={'Cache-Control':'no-store'})

    @app.get('/sabrina_wsp_research.js')
    def script():return FileResponse(ROOT/'sabrina_wsp_research.js',media_type='application/javascript',headers={'Cache-Control':'no-store'})
    return app


def main():
    import uvicorn
    parser=argparse.ArgumentParser();parser.add_argument('--port',type=int,default=8096);args=parser.parse_args()
    uvicorn.run(create_app(port=args.port),host='127.0.0.1',port=args.port)


if __name__=='__main__':main()
