from django.urls import path
from . import views

app_name = 'induction'

urlpatterns = [
    # Dashboard
    path('', views.DashboardView.as_view(), name='dashboard'),

    # Induction Que
    path('questions/', views.QuestionListView.as_view(), name='question_list'),
    path('questions/add/', views.QuestionCreateView.as_view(), name='question_create'),
    path('questions/bulk-add/', views.QuestionBulkCreateView.as_view(), name='question_bulk_create'),
    path('questions/<int:pk>/', views.QuestionDetailView.as_view(), name='question_view'),
    path('questions/<int:pk>/edit/', views.QuestionUpdateView.as_view(), name='question_edit'),
    path('questions/<int:pk>/delete/', views.QuestionDeleteView.as_view(), name='question_delete'),

    # Induction Video
    path('videos/', views.VideoListView.as_view(), name='video_list'),
    path('videos/add/', views.VideoCreateView.as_view(), name='video_create'),
    path('videos/<int:pk>/edit/', views.VideoUpdateView.as_view(), name='video_edit'),
    path('videos/<int:pk>/delete/', views.VideoDeleteView.as_view(), name='video_delete'),

    # Assign Que
    path('assign/', views.AssignListView.as_view(), name='assign_list'),
    path('assign/add/', views.AssignCreateView.as_view(), name='assign_create'),
    path('assign/<int:pk>/', views.AssignDetailView.as_view(), name='assign_detail'),
    path('assign/<int:pk>/edit/', views.AssignUpdateView.as_view(), name='assign_edit'),
    path('assign/<int:pk>/delete/', views.AssignDeleteView.as_view(), name='assign_delete'),
    path('assign/qr/info/', views.QRInfoView.as_view(), name='qr_info'),
    path('assign/qr/<int:plant_id>/image/', views.QRImageView.as_view(), name='qr_image'),

    # Scorecard
    path('scorecard/', views.ScorecardListView.as_view(), name='scorecard_list'),
    path('scorecard/export/', views.ScorecardExportView.as_view(), name='scorecard_export'),

    # Public QR portal (no login)
    path('portal/<str:token>/',         views.PortalView.as_view(),        name='portal'),
    path('portal/<str:token>/content/', views.PortalContentView.as_view(), name='portal_content'),
    path('portal/<str:token>/start/',   views.PortalStartView.as_view(),   name='portal_start'),
    path('portal/<str:token>/submit/',  views.PortalSubmitView.as_view(),  name='portal_submit'),
    path('qr/image/<int:plant_id>/', views.QRImageView.as_view(), name='qr_image'),
    path('qr/download/<int:plant_id>/', views.QRPosterDownloadView.as_view(), name='qr_download'),
    path('scorecard/<int:pk>/', views.ScorecardDetailView.as_view(), name='scorecard_detail'),
    path('portal/<str:token>/employee-lookup/', views.portal_employee_lookup, name='portal_employee_lookup'),
]